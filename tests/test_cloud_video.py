import json
import sys
import threading
from types import SimpleNamespace

import pytest

from backend import deploy_remote
from backend.bundles import Bundles
from backend.manager import Manager
from backend.store import Store
from tests.fake_colab import PROFILES, FakeColab
from tests.test_lifecycle import eventually


def test_media_stages_keep_parallel_completion_and_start_time(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy_remote, 'ROOT', tmp_path)
    deploy_remote.status('checking', '开始', reset=True)
    original = json.loads((tmp_path / 'deploy-status.json').read_text())
    barrier = threading.Barrier(2, timeout=2)
    def write(name):
        barrier.wait()
        deploy_remote.stage(name, 'completed', bytes=100)
    first = threading.Thread(target=write, args=('environment',))
    second = threading.Thread(target=write, args=('download',))
    first.start()
    second.start()
    first.join()
    second.join()
    deploy_remote.stage('download', 'running', bytes=10)
    deploy_remote.status('starting', '启动 ComfyUI')
    result = json.loads((tmp_path / 'deploy-status.json').read_text())
    assert result['started_at'] == original['started_at']
    assert result['stages']['environment']['status'] == result['stages']['download']['status'] == 'completed'
    assert result['stages']['download']['bytes'] == 100


@pytest.mark.parametrize('model', ['minimax-h3', 'minimax-image-bf16'])
async def test_public_models_start_without_drive_catalog_or_authorization(tmp_path, model):
    store, fake = Store(tmp_path), FakeColab()
    manager = Manager(store, fake, PROFILES, bundles=Bundles(tmp_path))
    await manager.open()
    try:
        manager.request_start('colab2', model)
        await eventually(lambda: manager.machine('colab2')['phase'] == 'ready')
        bootstrap = (tmp_path / 'colab2-bootstrap.py').read_text()
        assert 'https://huggingface.co/' in bootstrap
        assert "(root/'bundle-config.json').unlink" in bootstrap
        assert len([call for call in fake.calls if call[1] == 'create']) == 1
    finally:
        await manager.close()
        store.close()


@pytest.mark.parametrize('model', ['minimax-h3', 'minimax-image-bf16'])
def test_video_install_overlaps_restore_and_serving_waits_for_both(tmp_path, monkeypatch, model):
    import json
    (tmp_path / 'media-config.json').write_text(json.dumps({'model': model}))
    barrier = threading.Barrier(2, timeout=3)
    completed = set()

    def download():
        barrier.wait()
        completed.add('weights')

    def environment():
        barrier.wait()
        completed.add('environment')

    def launch(args):
        assert '--download-and-serve' in args
        assert completed == {'weights', 'environment'}

    monkeypatch.setitem(sys.modules, 'torch', SimpleNamespace(
        cuda=SimpleNamespace(is_available=lambda: True, get_device_capability=lambda: (12, 0)),
        version=SimpleNamespace(cuda='13.0'),
    ))
    monkeypatch.setattr(deploy_remote, 'ROOT', tmp_path)
    monkeypatch.setattr(deploy_remote, 'healthy', lambda: None)
    monkeypatch.setattr(deploy_remote, 'restore_cloud_weights', download)
    monkeypatch.setattr(deploy_remote, 'install_environment', environment)
    monkeypatch.setattr(deploy_remote, 'run', launch)
    deploy_remote.main()


def test_video_cloud_weights_link_into_comfy_after_checkout_and_reuse(tmp_path, monkeypatch):
    (tmp_path / 'bundle-config.json').write_text('{}')
    source = tmp_path / 'weights/vae/video.safetensors'
    source.parent.mkdir(parents=True)
    source.write_bytes(b'verified source')
    records = [{'file': 'vae/video.safetensors', 'target': 'weights/vae/video.safetensors'}]
    monkeypatch.setattr(deploy_remote, 'ROOT', tmp_path)
    monkeypatch.setattr(deploy_remote, 'COMFY', tmp_path / 'ComfyUI')
    monkeypatch.setattr(deploy_remote, 'restore_cloud_weights', lambda: records)
    assert deploy_remote.download_models() == records
    assert deploy_remote.download_models() == records
    assert (tmp_path / 'ComfyUI/models/vae/video.safetensors').read_bytes() == b'verified source'


async def test_broken_optional_drive_tunnel_does_not_block_hf_start(tmp_path, monkeypatch):
    store, fake = Store(tmp_path), FakeColab()
    bundles = Bundles(tmp_path)
    monkeypatch.setattr(bundles, 'optional_lease', lambda *_: {'base': 'synthetic', 'local_port': 12345})
    manager = Manager(store, fake, PROFILES, bundles=bundles)
    native_tunnel, calls = fake.tunnel, []

    async def tunnel(*args, **kwargs):
        calls.append(kwargs)
        if kwargs.get('reverse_port'):
            raise RuntimeError('Reverse forwarding unavailable')
        return await native_tunnel(*args, **kwargs)

    monkeypatch.setattr(fake, 'tunnel', tunnel)
    await manager.open()
    try:
        manager.request_start('colab2')
        await eventually(lambda: manager.machine('colab2')['phase'] == 'ready')
        assert calls == [{'reverse_port': 12345}, {}]
        bootstrap = (tmp_path / 'colab2-bootstrap.py').read_text()
        assert "(root/'bundle-config.json').unlink" in bootstrap
        assert 'https://huggingface.co/' in bootstrap
        assert len([call for call in fake.calls if call[1] == 'create']) == 1
    finally:
        await manager.close()
        store.close()
