import json

import pytest

from backend.config import ROOT
from backend.manager import Manager
from backend.media import graph_model
from backend.store import Store
from backend.workflows import validate_graph
from tests.fake_colab import PROFILES, FakeColab
from tests.test_lifecycle import eventually


def image_graph():
    return json.loads((ROOT / 'templates/h3-image-bf16.json').read_text())


async def test_bf16_profile_uses_public_weights_without_drive_and_preserves_graph(tmp_path):
    from backend.bundles import Bundles
    graph = json.loads((ROOT / 'templates/h3-image-bf16.json').read_text())
    assert graph_model(graph) == 'minimax-image-bf16'
    store, fake = Store(tmp_path), FakeColab(delay=.05)
    manager = Manager(store, fake, PROFILES, Bundles(tmp_path))
    await manager.open()
    try:
        job = manager.submit('colab2', graph, 'BF16', False)
        await eventually(lambda: manager.job(job['id'])['status'] == 'completed')
        assert manager.machine('colab2')['loaded_models'] == ['minimax-image-bf16']
        bootstrap = (tmp_path / 'colab2-bootstrap.py').read_text()
        assert 'public-weights.json' in bootstrap and 'https://huggingface.co/' in bootstrap
        assert "(root/'bundle-config.json').unlink" in bootstrap
        assert fake.prompt_posts[0][1]['prompt']['2']['inputs']['clip_name'].endswith('_bf16.safetensors')
    finally:
        await manager.close()
        store.close()


def test_image_resolution_rejects_low_res_and_checks_area():
    graph = image_graph()
    assert graph_model(validate_graph(graph)) == 'minimax-image-bf16'
    graph['4']['inputs'].update(width=1024, height=1024)
    with pytest.raises(ValueError, match='4–20 MP'):
        validate_graph(graph)
    graph['4']['inputs'].update(width=4096, height=4096)
    validate_graph(graph)
    graph['4']['inputs'].update(width=8192, height=8192)
    with pytest.raises(ValueError, match='4–20 MP'):
        validate_graph(graph)


async def test_image_then_video_reuses_vm_and_retains_both_models(tmp_path):
    store, fake = Store(tmp_path), FakeColab()
    manager = Manager(store, fake, PROFILES)
    await manager.open()
    try:
        manager.request_start('colab2', 'minimax-image-bf16')
        await eventually(lambda: manager.machine('colab2')['phase'] == 'ready')
        assert manager.machine('colab2')['loaded_models'] == ['minimax-image-bf16']
        session = manager.machine('colab2')['session']
        bootstrap = (tmp_path / 'colab2-bootstrap.py').read_text()
        assert 'minimax-image-bf16' in bootstrap
        manager.request_start('colab2', 'minimax-h3')
        await manager.start_tasks['colab2']
        assert manager.machine('colab2')['phase'] == 'ready'
        assert set(manager.machine('colab2')['loaded_models']) == {'minimax-h3', 'minimax-image-bf16'}
        assert manager.machine('colab2')['session'] == session
        assert len([call for call in fake.calls if call[1] == 'create']) == 1
        assert fake.active['colab1'][0]['name'] == 'unrelated-important-session'
    finally:
        await manager.close()
        store.close()


async def test_image_submission_uses_hf_without_drive(tmp_path):
    from backend.bundles import Bundles
    store, fake = Store(tmp_path), FakeColab()
    manager = Manager(store, fake, PROFILES, Bundles(tmp_path))
    await manager.open()
    try:
        job = manager.submit('colab2', image_graph(), 'image', False)
        await eventually(lambda: manager.job(job['id'])['status'] == 'completed')
        assert manager.job(job['id'])['outputs'][0]['filename'].endswith('.png')
    finally:
        await manager.close()
        store.close()


async def test_image_queued_during_video_start_waits_and_reuses_vm(tmp_path):
    import asyncio
    store, fake = Store(tmp_path), FakeColab(delay=.05)
    manager = Manager(store, fake, PROFILES)
    await manager.open()
    fake.create_gate = asyncio.Event()
    try:
        manager.request_start('colab2', 'minimax-h3')
        await eventually(lambda: any(c[1] == 'create' for c in fake.calls))
        job = manager.submit('colab2', image_graph(), 'queued image', False)
        fake.create_gate.set()
        await eventually(lambda: manager.job(job['id'])['status'] == 'completed')
        assert manager.job(job['id'])['outputs'][0]['filename'].endswith('.png')
        assert set(manager.machine('colab2')['loaded_models']) == {'minimax-h3', 'minimax-image-bf16'}
        assert len([c for c in fake.calls if c[1] == 'create']) == 1
    finally:
        fake.create_gate.set()
        await manager.close()
        store.close()


async def test_retired_image_graph_rejected_before_allocation(tmp_path):
    graph = image_graph()
    graph['1']['inputs']['unet_name'] = 'minimax_h3_image_pruned_int8_convrot.safetensors'
    graph['2']['inputs']['clip_name'] = 'qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors'
    store, fake = Store(tmp_path), FakeColab()
    manager = Manager(store, fake, PROFILES)
    try:
        with pytest.raises(ValueError, match='仅支持 BF16'):
            manager.submit('colab2', graph, 'retired', False)
        assert not fake.calls and not store.all('jobs')
    finally:
        store.close()
