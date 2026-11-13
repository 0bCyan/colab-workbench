import configparser
import datetime
import hashlib
import http.server
import io
import json
import tarfile
import threading
import time

import aiohttp
import pytest

from backend.bundle_restore_remote import prepare_in_parallel, restore, safe_path
from backend.bundles import LLM_BUNDLE, Bundles


def test_environment_and_weight_download_start_in_parallel():
    barrier = threading.Barrier(2, timeout=3)
    def download():
        barrier.wait()
        return 'weights verified'
    def environment():
        barrier.wait()
    assert prepare_in_parallel(download, environment) == 'weights verified'
    with pytest.raises(RuntimeError, match='environment'):
        prepare_in_parallel(lambda: None, lambda: (_ for _ in ()).throw(RuntimeError('environment')))


@pytest.mark.parametrize('name', ['../outside', '/outside', '', 'sub/../../outside', 'sub\\outside'])
def test_restore_paths_cannot_escape_root(tmp_path, name):
    with pytest.raises(ValueError, match='Unsafe'):
        safe_path(tmp_path, name)


async def test_download_broker_limits_catalog_and_revokes_grants(tmp_path, monkeypatch):
    bundles = Bundles(tmp_path)
    bundles.directory.mkdir()
    index = {'preset': LLM_BUNDLE, 'complete': True, 'deployment_revision': 3,
             'files': [{'name': f'synthetic{i}', 'file_id': f'fake{i}', 'target': f'weights/{i}',
                        'bytes': 1, 'sha256': hashlib.sha256(b'x').hexdigest()} for i in range(7)], 'total_bytes': 7}
    (bundles.directory / (LLM_BUNDLE + '.json')).write_text(json.dumps(index))
    config = configparser.ConfigParser()
    config['reader'] = {'scope': 'drive.readonly', 'token': json.dumps({'access_token': 'fake', 'expiry':
                         (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)).isoformat()})}
    path = tmp_path / 'reader.conf'
    with path.open('w') as stream:
        config.write(stream)
    monkeypatch.setattr(bundles, 'reader_config', lambda: path)
    monkeypatch.setattr(bundles, 'rclone', lambda: 'synthetic')
    await bundles.open()
    try:
        grant = bundles.lease('instance', [LLM_BUNDLE])
        nonce = grant['base'].rsplit('/', 1)[1]
        base = f'http://127.0.0.1:{bundles.port}/{nonce}'
        async with aiohttp.ClientSession() as client:
            async with client.get(base + '/' + LLM_BUNDLE + '.json') as response:
                assert response.status == 200 and await response.json() == index
            async with client.get(base + '/token') as response:
                assert (await response.json())['scope'].endswith('drive.readonly')
            async with client.get(base + '/other.json') as response:
                assert response.status == 404
            bundles.revoke('instance')
            async with client.get(base + '/token') as response:
                assert response.status == 404
        assert not any('fake' in str(item) for item in bundles.public())
        config['reader']['scope'] = 'drive'
        with path.open('w') as stream:
            config.write(stream)
        with pytest.raises(ValueError, match='readonly'):
            bundles.require([LLM_BUNDLE])
    finally:
        await bundles.close()


def test_stream_restore_checks_hashes_reuses_verified_files_and_rejects_corruption(tmp_path):
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode='w:gz') as archive:
        item = tarfile.TarInfo('runtime/hello.txt')
        item.size = 5
        archive.addfile(item, io.BytesIO(b'hello'))
    blobs = {'runtime': raw.getvalue(), 'weight': b'weights' * 10000}
    files = [{'name': name + '.tar.gz' if name == 'runtime' else name, 'file_id': name, 'target': 'weights/model.bin' if name == 'weight' else '.',
              'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
              **({'archive': 'tar.gz'} if name == 'runtime' else {})} for name, data in blobs.items()]
    index = {'preset': LLM_BUNDLE, 'complete': True, 'deployment_revision': 3, 'files': files,
             'total_bytes': sum(map(len, blobs.values()))}
    corrupt, reads = [False], []
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/index.json':
                data, status = json.dumps(index).encode(), 200
            elif self.path == '/token':
                data, status = json.dumps({'scope': 'https://www.googleapis.com/auth/drive.readonly',
                                           'access_token': 'fake', 'expires_at': time.time() + 3600}).encode(), 200
            else:
                name = self.path.rsplit('/', 1)[1].split('?')[0]
                assert self.headers['Authorization'] == 'Bearer fake'
                start, end = map(int, self.headers['Range'].removeprefix('bytes=').split('-'))
                data = blobs[name][start:end + 1]
                if corrupt[0] and name == 'weight':
                    data = bytes([data[0] ^ 1]) + data[1:]
                reads.append(name)
                status = 206
            self.send_response(status)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def log_message(self, *_args):
            pass
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        base = f'http://127.0.0.1:{server.server_port}'
        broker = tmp_path / 'grant.json'
        broker.write_text(json.dumps({'base': base}))
        kwargs = {'workers': 4, 'chunk_size': 16384, 'api_base': base + '/files/'}
        root = tmp_path / 'restored'
        result = restore(broker, root, **kwargs)
        assert result['downloaded_bytes'] == index['total_bytes']
        assert result['bytes'] == result['verified_bytes'] == index['total_bytes']
        assert result['files_done'] == result['files_total'] == 2
        assert result['rate_bytes_s'] > 0 and result['retries'] == 0
        assert (root / 'weights/model.bin').read_bytes() == blobs['weight']
        assert (root / 'runtime/hello.txt').read_text() == 'hello'
        reads.clear()
        cached = restore(broker, root, **kwargs)
        assert cached['downloaded_bytes'] == 0 and not reads
        assert cached['cached_bytes'] == cached['bytes'] == index['total_bytes']
        assert cached['files_done'] == 2
        wide = restore(broker, tmp_path / 'wide', workers=128, api_base=base + '/files/')
        assert wide['workers'] == 128 and wide['buffer_limit_bytes'] == 4 * 2**30
        corrupt[0] = True
        with pytest.raises(ValueError, match='SHA256'):
            restore(broker, tmp_path / 'corrupt', **kwargs)
        assert not (tmp_path / 'corrupt/weights/model.bin').exists()
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


@pytest.mark.parametrize('workers', [0, 257, True, 1.5])
def test_restore_rejects_invalid_parallelism_before_reading_credentials(tmp_path, workers):
    with pytest.raises(ValueError, match='workers'):
        restore(tmp_path / 'missing.json', tmp_path / 'out', workers=workers)


def test_public_pinned_weights_use_ranges_without_credentials_and_verify_sha256(tmp_path, monkeypatch):
    import requests
    payload = b'public model tensor bytes' * 1024
    digest = hashlib.sha256(payload).hexdigest()
    file = {'name': 'model', 'target': 'weights/model.safetensors', 'bytes': len(payload), 'sha256': digest,
            'url': 'https://huggingface.co/synthetic/test/resolve/' + 'a' * 40 + '/model.safetensors'}
    index = {'complete': True, 'deployment_revision': 2, 'files': [file], 'total_bytes': len(payload)}
    reads, corrupt = [], [False]
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            assert not self.headers.get('Authorization')
            start, end = map(int, self.headers['Range'][6:].split('-'))
            data = payload[start:end + 1]
            if corrupt[0]:
                data = bytes([data[0] ^ 1]) + data[1:]
            reads.append((start, end))
            self.send_response(206)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def log_message(self, *_args):
            pass
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    native_session = requests.Session
    class LocalSource:
        def __init__(self):
            self.http = native_session()
        def get(self, url, **kwargs):
            assert url == file['url']
            assert not kwargs['params'] and not kwargs['headers'].get('Authorization')
            return self.http.get(f'http://127.0.0.1:{server.server_port}/model', **kwargs)
    monkeypatch.setattr(requests, 'Session', LocalSource)
    try:
        root = tmp_path / 'public'
        restore(None, root, workers=3, chunk_size=1024, public_index=index)
        assert (root / file['target']).read_bytes() == payload
        assert len(reads) > 1
        reads.clear()
        result = restore(None, root, workers=3, public_index=index)
        assert not reads and result['cached_bytes'] == len(payload)
        corrupt[0] = True
        with pytest.raises(ValueError, match='SHA256'):
            restore(None, tmp_path / 'corrupt', workers=3, chunk_size=1024, public_index=index)
        assert not (tmp_path / 'corrupt' / file['target']).exists()
        with pytest.raises(ValueError, match='pinned'):
            restore(None, tmp_path / 'unversioned', public_index={**index, 'files': [{**file, 'url': file['url'].replace('a' * 40, 'main')}]})
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize('mode', ['hf', 'hf-no-fallback', 'fallback', 'corrupt', 'mismatch', 'drive-only'])
def test_hf_priority_and_optional_matching_drive_fallback(tmp_path, monkeypatch, mode):
    import requests
    blobs = {'large': b'large verified tensor' * 1024, 'small': b'small tensor' * 64}
    files = [{'name': name, 'target': 'weights/' + name, 'bytes': len(data),
              'sha256': hashlib.sha256(data).hexdigest(), 'file_id': name,
              'url': 'https://huggingface.co/synthetic/test/resolve/' + 'a' * 40 + '/' + name}
             for name, data in blobs.items()]
    index = {'preset': 'h3-video-g4-v1', 'complete': True, 'deployment_revision': 2, 'weights_only': True,
             'files': files, 'total_bytes': sum(map(len, blobs.values()))}
    mirror = {**index, 'files': [{**file} for file in files]}
    if mode == 'mismatch':
        mirror['files'][1]['sha256'] = '0' * 64
    reads = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split('?')[0]
            reads.append(path)
            if path.endswith('.json'):
                data, code = json.dumps(mirror).encode(), 200
            elif path == '/token':
                data, code = json.dumps({'scope': 'https://www.googleapis.com/auth/drive.readonly',
                                          'access_token': 'fake', 'expires_at': time.time() + 3600}).encode(), 200
            else:
                if path.startswith('/hf/'):
                    assert not self.headers.get('Authorization')
                else:
                    assert self.headers['Authorization'] == 'Bearer fake'
                name = path.rsplit('/', 1)[1]
                start, end = map(int, self.headers['Range'][6:].split('-'))
                data, code = blobs[name][start:end + 1], 206
                if path == '/hf/small':
                    if mode in {'fallback', 'mismatch', 'hf-no-fallback'}:
                        data, code = b'Unavailable', 403
                    elif mode == 'corrupt':
                        data = bytes([data[0] ^ 1]) + data[1:]
            self.send_response(code)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_args):
            pass

    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    native_session = requests.Session

    class LocalSource:
        def __init__(self):
            self.http = native_session()

        def get(self, url, **kwargs):
            if url.startswith('https://huggingface.co/'):
                url = base + '/hf/' + url.rsplit('/', 1)[1]
            return self.http.get(url, **kwargs)

    monkeypatch.setattr(requests, 'Session', LocalSource)
    broker = tmp_path / 'grant.json'
    broker.write_text(json.dumps({'base': base, 'bundle_id': index['preset']}))
    root = tmp_path / 'restored'
    try:
        options = {'workers': 2, 'chunk_size': 1024, 'api_base': base + '/drive/',
                   'public_index': None if mode == 'drive-only' else index}
        if mode == 'mismatch':
            with pytest.raises(RuntimeError, match='网盘兜底也不可用'):
                restore(broker, root, **options)
            assert not any(path.startswith('/drive/') for path in reads)
            assert not (root / 'weights/small').exists()
        elif mode == 'hf-no-fallback':
            with pytest.raises(PermissionError, match='403'):
                restore(None, root, **options)
            assert all(path.startswith('/hf/') for path in reads)
        else:
            result = restore(broker, root, **options)
            for name, data in blobs.items():
                assert (root / 'weights' / name).read_bytes() == data
            assert result['source'] == ('hf' if mode == 'hf' else 'drive')
            assert result['bytes'] == result['verified_bytes'] == index['total_bytes']
            if mode == 'hf':
                assert all(path.startswith('/hf/') for path in reads)
            elif mode == 'drive-only':
                assert not any(path.startswith('/hf/') for path in reads)
            else:
                assert '/token' in reads and '/drive/small' in reads
                assert '/drive/large' not in reads  # Completed HF files remain verified and cached.
                assert result['cached_bytes'] == len(blobs['large'])
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_optional_mirror_does_not_require_drive_and_rejects_different_weights(tmp_path, monkeypatch):
    bundles = Bundles(tmp_path)
    monkeypatch.setattr(bundles, 'reader_config', lambda: tmp_path / 'missing-reader.conf')
    key = 'h3-video-g4-v1'
    assert bundles.optional_lease('owner', key, {}) is None
    assert bundles.optional_lease('owner', None, {}) is None
    index = {'preset': key, 'complete': True, 'deployment_revision': 2, 'weights_only': True, 'total_bytes': 1,
             'files': [{'name': 'model', 'target': 'weights/model', 'bytes': 1, 'sha256': 'a' * 64, 'file_id': 'fake'}]}
    bundles.directory.mkdir()
    (bundles.directory / (key + '.json')).write_text(json.dumps(index))
    assert bundles.optional_lease('owner', key, index) is None  # No reader authorization.
    calls = []
    monkeypatch.setattr(bundles, 'lease', lambda *args: calls.append(args) or {'base': 'synthetic'})
    assert bundles.optional_lease('owner', key, index) == {'base': 'synthetic'}
    calls.clear()
    other = {**index, 'files': [{**index['files'][0], 'sha256': 'b' * 64}]}
    assert bundles.optional_lease('owner', key, other) is None
    assert not calls
