"""Bounded parallel reads, ordered streaming SHA256, no intermediate PLE pieces.

Use a dedicated tmpfs root and --mmap-experts for the experimental RAM layout.
Logical files are atomically committed only after full checksum verification.
Partial files restart; the older restore_direct.py remains the disk/resume path.
"""
import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import tarfile
import threading
import time
from collections import deque
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

import requests


class ChecksumError(ValueError):
    """Downloaded bytes did not match the pinned model."""


def prepare_in_parallel(download, environment):
    """Configure the interpreter while the independent weight transfer is running."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        transfers = pool.submit(download)
        setup = pool.submit(environment)
        for future in concurrent.futures.as_completed([transfers, setup]):
            future.result()
        return transfers.result()

def safe_path(root, name):
    if not isinstance(name, str) or not name or '\\' in name:
        raise ValueError('Unsafe relative path')
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts:
        raise ValueError('Unsafe relative path')
    return root / path

def restore(broker, root, workers=64, chunk_size=64 << 20, api_base='https://www.googleapis.com/drive/v3/files/', public_index=None):
    """Use HF first; only consult an optional, identical Drive mirror after failure."""
    options = {'workers': workers, 'chunk_size': chunk_size, 'api_base': api_base}
    try:
        return _restore(broker, root, public_index=public_index, **options)
    except (requests.RequestException, RuntimeError, PermissionError, ChecksumError):
        if public_index is None or broker is None:
            raise
        try:
            grant = json.loads(Path(broker).read_text())
            response = requests.get(grant['base'].rstrip('/') + '/' + grant['bundle_id'] + '.json', timeout=(5, 30))
            response.raise_for_status()
            fallback = response.json()
            def identity(index):
                return sorted((item['target'], item['bytes'], item['sha256']) for item in index['files'])
            if (not fallback.get('weights_only') or fallback.get('assemblies')
                    or any(item.get('archive') for item in fallback['files'])
                    or identity(fallback) != identity(public_index)):
                raise ValueError('Drive fallback must match pinned HF weights')
            print('HF download failed; using verified Drive mirror', flush=True)
            # Keep the validated catalog fixed; previously verified files are reused.
            return _restore(broker, root, drive_index=fallback, **options)
        except Exception as fallback_error:
            raise RuntimeError('HF 下载失败，网盘兜底也不可用；请检查下载源与网盘授权') from fallback_error


def _restore(broker, root, workers=64, chunk_size=64 << 20, api_base='https://www.googleapis.com/drive/v3/files/', public_index=None, drive_index=None):
    if type(workers) is not int or not 1 <= workers <= 256:
        raise ValueError('workers must be between 1 and 256')
    if type(chunk_size) is not int or chunk_size < 1:
        raise ValueError('chunk_size must be positive')
    # Extra concurrency must not multiply the verified four-GiB payload buffer.
    chunk_size = min(chunk_size, (4 << 30) // workers)
    started = time.perf_counter()
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    grant = json.loads(Path(broker).read_text()) if public_index is None else {}
    base = grant.get('base', '').rstrip('/')
    auth, auth_lock, local = {}, threading.Lock(), threading.local()
    metrics_lock = threading.Lock()
    status_path = root.parent / 'stream-restore-status.json'
    summary = {'files': [], 'downloaded_bytes': 0, 'cached_bytes': 0, 'verified_bytes': 0,
               'files_done': 0, 'workers': workers, 'buffer_limit_bytes': workers*chunk_size,
               'requests': 0, 'retries': 0, 'http_errors': {}, 'source': 'hf' if public_index is not None else 'drive'}

    def remote_json(name):
        for attempt in range(12):
            try:
                response = requests.get(base+'/'+name, timeout=(5, 30))
                response.raise_for_status()
                return response.json()
            except requests.ConnectionError:
                if attempt == 11:
                    raise
                time.sleep(min(.5 * (attempt + 1), 2))

    index = public_index if public_index is not None else drive_index if drive_index is not None else remote_json(grant.get('bundle_id', 'index') + '.json')
    summary['total_bytes'] = index['total_bytes']
    if not index.get('complete') or index.get('deployment_revision', 0) < 2:
        raise ValueError('Complete deployment revision 2+ required')
    for job in [*index['files'], *index.get('assemblies', [])]:
        safe_path(root, job['target'])
        if type(job['bytes']) is not int or job['bytes'] <= 0 or not re.fullmatch('[0-9a-f]{64}', job['sha256']):
            raise ValueError('Invalid weight size or digest')
        if public_index is not None:
            source = urlparse(job.get('url', ''))
            if source.scheme != 'https' or source.netloc != 'huggingface.co' or not re.search(r'/resolve/[0-9a-f]{40}/', source.path):
                raise ValueError('Public weights require pinned Hugging Face URLs')
    if len({job['name'] for job in index['files']}) != len(index['files']):
        raise ValueError('Duplicate artifact name')

    def save(stage):
        summary['stage'] = stage
        summary['elapsed_seconds'] = time.perf_counter()-started
        summary['bytes'] = summary['downloaded_bytes'] + summary['cached_bytes']
        summary['rate_bytes_s'] = summary['downloaded_bytes'] / max(.001, summary['elapsed_seconds'])
        summary['time'] = time.time()
        temporary = status_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(summary))
        temporary.replace(status_path)

    def headers():
        if public_index is not None:
            return {}
        with auth_lock:
            if auth.get('expires_at', 0) < time.time()+30:
                auth.update(remote_json('token'))
                if auth['scope'] != 'https://www.googleapis.com/auth/drive.readonly':
                    raise ValueError('Read-only grant required')
            return {'Authorization': 'Bearer '+auth['access_token']}

    def fetch(task):
        job, start, length = task
        if not hasattr(local, 'http'):
            local.http = requests.Session()
        for attempt in range(3):
            try:
                with metrics_lock:
                    summary['requests'] += 1
                with local.http.get(job['url'] if public_index is not None else api_base+job['file_id'], params={} if public_index is not None else {'alt':'media'},
                                    headers=dict(headers(), Range=f'bytes={start}-{start+length-1}'),
                                    stream=True, timeout=(15, 40)) as response:
                    if response.status_code != 206:
                        with metrics_lock:
                            key = str(response.status_code)
                            summary['http_errors'][key] = summary['http_errors'].get(key, 0) + 1
                        if response.status_code in (403, 429):
                            raise PermissionError('Weight download HTTP ' + str(response.status_code))
                        raise RuntimeError('Weight download HTTP '+str(response.status_code))
                    data = bytearray()
                    for block in response.iter_content(1 << 20):
                        data.extend(block)
                        if len(data) > length:
                            raise ChecksumError('Oversized range')
                    if len(data) != length:
                        raise ChecksumError('Truncated range')
                    return data
            except PermissionError:
                raise
            except Exception:
                if attempt == 2:
                    raise
                with metrics_lock:
                    summary['retries'] += 1
                time.sleep(1+attempt)

    files = {job['name']:job for job in index['files']}
    assemblies = index.get('assemblies', [])
    part_names = {name for assembly in assemblies for name in assembly['parts']}
    logical = [(job, [job]) for job in files.values() if job['name'] not in part_names]
    for assembly in assemblies:
        parts = [files[name] for name in assembly['parts']]
        position = 0
        for part in parts:
            if part['source_offset'] != position or part['source_target'] != assembly['target']:
                raise ValueError('Noncontiguous assembly')
            position += part['bytes']
        if position != assembly['bytes']:
            raise ValueError('Assembly size mismatch')
        logical.append((assembly, parts))
    # The small runtime goes first; each remaining logical file uses all transfer workers.
    logical.sort(key=lambda item: (not bool(item[0].get('archive')), -item[0]['bytes']))
    summary['files_total'] = len(logical)
    for target, parts in logical:
        relative = target['name'] if target.get('archive') else target['target']
        summary['current'] = relative
        path = safe_path(root, relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        verified = root / '.bundle-verified' / target['sha256']
        if verified.is_file() and path.is_file():
            try:
                stamp = json.loads(verified.read_text())
                stat = path.stat()
                if stamp == {'bytes': target['bytes'], 'mtime_ns': stat.st_mtime_ns} and stat.st_size == target['bytes']:
                    summary['cached_bytes'] += target['bytes']
                    summary['verified_bytes'] += target['bytes']
                    summary['files_done'] += 1
                    save('cached ' + relative)
                    continue
            except (ValueError, OSError):
                pass
        temporary = path.with_name(path.name+'.stream-part')
        whole_sha = hashlib.sha256()
        is_assembly = len(parts) > 1
        begin = time.perf_counter()
        tasks = ((job, start, min(chunk_size, job['bytes']-start))
                 for job in parts for start in range(0, job['bytes'], chunk_size))
        current_job, part_sha, part_size, written = None, None, 0, 0

        def verify_part():
            if current_job is None:
                return
            if part_size != current_job['bytes'] or (is_assembly and part_sha.hexdigest() != current_job['sha256']):
                raise ChecksumError('Part SHA256 mismatch: '+current_job['name'])

        save('downloading '+relative)
        with temporary.open('wb', buffering=0) as stream, concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            pending = deque()
            for _ in range(workers):
                task = next(tasks, None)
                if task is not None:
                    pending.append((task, pool.submit(fetch, task)))
            while pending:
                task, future = pending.popleft()
                job, _, _ = task
                data = future.result()
                del future
                if current_job is not job:
                    verify_part()
                    current_job, part_sha, part_size = job, hashlib.sha256(), 0
                whole_sha.update(data)
                if is_assembly:
                    part_sha.update(data)
                view = memoryview(data)
                while view:
                    count = stream.write(view)
                    if not count:
                        raise IOError('Short write')
                    view = view[count:]
                written += len(data)
                part_size += len(data)
                summary['downloaded_bytes'] += len(data)
                del data, view
                task = next(tasks, None)
                if task is not None:
                    pending.append((task, pool.submit(fetch, task)))
                save('downloading '+relative)
            verify_part()
            os.fsync(stream.fileno())
        if written != target['bytes'] or whole_sha.hexdigest() != target['sha256']:
            raise ChecksumError('Logical file SHA256 mismatch: '+relative)
        temporary.replace(path)
        row = {'target':relative, 'bytes':written, 'seconds':time.perf_counter()-begin, 'sha256_verified':True}
        row['MB_s'] = written/row['seconds']/1e6
        summary['files'].append(row)
        print(json.dumps(row), flush=True)
        if target.get('archive'):
            with tarfile.open(path) as archive:
                for member in archive.getmembers():
                    safe_path(root, member.name)
                    if member.issym() or member.islnk():
                        raise ValueError('Archive links are not allowed')
                archive.extractall(root, filter='data')
            (root / '.runtime-ready').touch()
        verified.parent.mkdir(exist_ok=True)
        verified.write_text(json.dumps({'bytes': path.stat().st_size, 'mtime_ns': path.stat().st_mtime_ns}))
        summary['verified_bytes'] += target['bytes']
        summary['files_done'] += 1
        save('verified ' + relative)
    (root/'bundle-manifest.json').write_text(json.dumps(index))
    summary['total_bytes'] = index['total_bytes']
    summary['restored'] = True
    summary['current'] = None
    save('restored')
    (root.parent/'stream-restore-timings.json').write_text(json.dumps(summary, indent=2))
    return summary

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--broker', type=Path, required=True)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--workers', type=int, default=64)
    parser.add_argument('--mmap-experts', action='store_true', help='Use with the RAM layout to avoid a duplicate resident expert arena')
    args = parser.parse_args()
    if not 1 <= args.workers <= 256:
        parser.error('workers must be between 1 and 256')
    restore(args.broker, args.root, args.workers)
    if args.mmap_experts:
        path = args.root/'preset.json'
        preset = json.loads(path.read_text())
        preset['engine_options']['mmap-experts'] = True
        path.write_text(json.dumps(preset, indent=2))
