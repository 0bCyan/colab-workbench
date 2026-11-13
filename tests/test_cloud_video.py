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


async def test_missing_video_catalog_fails_before_any_allocation(tmp_path):
    store, fake = Store(tmp_path), FakeColab()
    manager = Manager(store, fake, PROFILES, bundles=Bundles(tmp_path))
    try:
        with pytest.raises(ValueError, match='云盘部署包尚未配置'):
            manager.request_start('colab2')
        assert not fake.calls and not manager.start_tasks
    finally:
        store.close()


@pytest.mark.parametrize('model', ['minimax-h3', 'minimax-image-bf16'])
def test_video_install_overlaps_restore_and_serving_waits_for_both(tmp_path, monkeypatch, model):
    import json
    (tmp_path / 'media-config.json').write_text(json.dumps({'model': model}))
    if model == 'minimax-h3':
        (tmp_path / 'bundle-config.json').write_text('{}')
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
