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

import requests


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

def restore(broker, root, workers=64, chunk_size=64 << 20, api_base='https://www.googleapis.com/drive/v3/files/'):
    started = time.perf_counter()
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    grant = json.loads(Path(broker).read_text())
    base = grant['base'].rstrip('/')
    auth, auth_lock, local = {}, threading.Lock(), threading.local()
    status_path = root.parent / 'stream-restore-status.json'
    summary = {'files': [], 'downloaded_bytes': 0, 'workers': workers, 'buffer_limit_bytes': workers*chunk_size}

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

    index = remote_json(grant.get('bundle_id', 'index') + '.json')
    summary['total_bytes'] = index['total_bytes']
    if not index.get('complete') or index.get('deployment_revision', 0) < 2:
        raise ValueError('Complete deployment revision 2+ required')
    for job in [*index['files'], *index.get('assemblies', [])]:
        safe_path(root, job['target'])
        if type(job['bytes']) is not int or job['bytes'] <= 0 or not re.fullmatch('[0-9a-f]{64}', job['sha256']):
            raise ValueError('Invalid weight size or digest')
    if len({job['name'] for job in index['files']}) != len(index['files']):
        raise ValueError('Duplicate artifact name')

    def save(stage):
        summary['stage'] = stage
        summary['elapsed_seconds'] = time.perf_counter()-started
        temporary = status_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(summary))
        temporary.replace(status_path)

    def headers():
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
                with local.http.get(api_base+job['file_id'], params={'alt':'media'},
                                    headers=dict(headers(), Range=f'bytes={start}-{start+length-1}'),
                                    stream=True, timeout=(15, 40)) as response:
                    if response.status_code != 206:
                        raise RuntimeError('Drive HTTP '+str(response.status_code))
                    data = bytearray()
                    for block in response.iter_content(1 << 20):
                        data.extend(block)
                        if len(data) > length:
                            raise ValueError('Oversized range')
                    if len(data) != length:
                        raise ValueError('Truncated range')
                    return data
            except Exception:
                if attempt == 2:
                    raise
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
    for target, parts in logical:
        relative = target['name'] if target.get('archive') else target['target']
        path = safe_path(root, relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        verified = root / '.bundle-verified' / target['sha256']
        if verified.is_file() and path.is_file():
            try:
                stamp = json.loads(verified.read_text())
                stat = path.stat()
                if stamp == {'bytes': target['bytes'], 'mtime_ns': stat.st_mtime_ns} and stat.st_size == target['bytes']:
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
                raise ValueError('Part SHA256 mismatch: '+current_job['name'])

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
            raise ValueError('Logical file SHA256 mismatch: '+relative)
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
    (root/'bundle-manifest.json').write_text(json.dumps(index))
    summary['total_bytes'] = index['total_bytes']
    summary['restored'] = True
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
    if not 1 <= args.workers <= 64:
        parser.error('workers must be between 1 and 64')
    restore(args.broker, args.root, args.workers)
    if args.mmap_experts:
        path = args.root/'preset.json'
        preset = json.loads(path.read_text())
        preset['engine_options']['mmap-experts'] = True
        path.write_text(json.dumps(preset, indent=2))
