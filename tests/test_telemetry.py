import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from backend.manager import Manager
from backend.store import Store
from backend.telemetry_remote import Sampler, gpu_sample
from backend.telemetry_remote import deployment_status as read_deployment
from tests.fake_colab import PROFILES, FakeColab


async def test_deployment_and_metrics_share_one_read_and_update_one_snapshot(monitor):
    manager, fake = monitor
    await manager.start('colab2')
    manager.telemetry.cache.clear()
    fake.metric_reads = 0
    started = time.time()
    manager.machine('colab2', phase='deploying', loading_started_at=started, deployment=None)
    state = {'phase': 'downloading', 'started_at': started, 'time': started, 'message': '下载权重',
             'stages': {'download': {'status': 'running', 'bytes': 64, 'total_bytes': 128}}}
    fake.deployment_samples['colab2'] = state
    calls = len(fake.calls)
    result, deployment = await asyncio.gather(manager.telemetry.read('colab2'), manager.telemetry.deployment('colab2', 'media'))
    assert fake.metric_reads == 1 and len(fake.calls) == calls
    assert deployment == state == result['machine']['deployment']
    assert result['sample']['gpu']['percent'] == 73
    # A fresh performance sample can never revive an older deployment attempt.
    manager.telemetry.cache.clear()
    fake.deployment_samples['colab2'] = {**state, 'started_at': started - 30, 'phase': 'ready'}
    result = await manager.telemetry.read('colab2')
    assert result['deployment'] is None and manager.machine('colab2')['deployment'] == state
    manager.machine('colab2', phase='ready')


def test_sidecar_reads_only_known_progress_files_and_handles_atomic_replace(tmp_path, monkeypatch):
    from backend import telemetry_remote
    path = tmp_path / 'deploy-status.json'
    monkeypatch.setattr(telemetry_remote, 'DEPLOYMENTS', {'/deployment/media': path})
    assert read_deployment('/deployment/media') is None
    assert read_deployment('/deployment/../../secrets') is None
    path.write_text(json.dumps({'phase': 'downloading', 'stages': {'download': {'bytes': 10}}}))
    assert read_deployment('/deployment/media')['stages']['download']['bytes'] == 10
    path.write_text('{')
    assert read_deployment('/deployment/media') is None


@pytest.fixture
async def monitor(tmp_path):
    store, fake = Store(tmp_path), FakeColab()
    manager = Manager(store, fake, PROFILES)
    await manager.open()
    yield manager, fake
    await manager.close()
    store.close()


async def test_metrics_never_provision_and_isolate_account_session(monitor):
    manager, fake = monitor
    blank = await manager.telemetry.read('colab2')
    assert blank['status'] == 'offline' and blank['sample'] is None and blank['history'] == []
    assert fake.calls == []
    await manager.start('colab2')
    metrics = await manager.telemetry.read('colab2')
    assert metrics['status'] == 'live'
    assert metrics['sample']['gpu']['percent'] == 73
    assert len(metrics['history']) >= 1
    assert (await manager.telemetry.read('colab3'))['sample'] is None
    assert (await manager.telemetry.read('colab2')) == metrics
    session = manager.machine('colab2')['session']
    manager.machine('colab2', session='replacement', telemetry_port=None)
    metrics = await manager.telemetry.read('colab2')
    assert metrics['status'] == 'waiting' and metrics['sample'] is None and metrics['history'] == []
    manager.machine('colab2', session=session, telemetry_port=fake.telemetry_ports['colab2'])
    assert (await manager.telemetry.read('colab2'))['status'] == 'live'
    await manager.stop('colab2')
    metrics = await manager.telemetry.read('colab2')
    assert metrics['status'] == 'offline' and metrics['history'] == []


async def test_stale_or_disconnected_samples_are_never_shown_as_live(monitor):
    manager, fake = monitor
    await manager.start('colab2')
    fake.metric_offset = -100
    metrics = await manager.telemetry.read('colab2')
    assert metrics['status'] == 'unavailable' and metrics['sample'] is None
    manager.machine('colab2', phase='disconnected')
    assert (await manager.telemetry.read('colab2'))['status'] == 'disconnected'
    manager.machine('colab2', phase='ready')
    fake.metric_offset = 0
    manager.telemetry.cache.clear()
    assert (await manager.telemetry.read('colab2'))['status'] == 'live'


def test_nvidia_mib_conversion_and_unsupported_fields(monkeypatch):
    monkeypatch.setattr('backend.telemetry_remote.subprocess.run', lambda *a, **kw: SimpleNamespace(
        stdout='NVIDIA RTX PRO 6000, 73, 38912, 98304, 58, [N/A], 600\n'))
    sample = gpu_sample()
    assert sample['memory_used'] == 38 * 2**30
    assert sample['memory_percent'] == pytest.approx(38 / 96 * 100)
    assert sample['power'] is None and sample['temperature'] == 58


def test_first_cpu_network_samples_unknown_and_loopback_excluded(monkeypatch):
    def missing_gpu():
        raise FileNotFoundError('no GPU')
    monkeypatch.setattr('backend.telemetry_remote.gpu_sample', missing_gpu)
    counters = {'eth0': SimpleNamespace(bytes_recv=1000, bytes_sent=500),
                'lo': SimpleNamespace(bytes_recv=1000000, bytes_sent=1000000)}
    fake = SimpleNamespace(cpu_percent=lambda **k: 20, cpu_count=lambda: 24,
        virtual_memory=lambda: SimpleNamespace(total=100, available=60, percent=40),
        disk_usage=lambda path: SimpleNamespace(total=100, used=30, percent=30),
        net_io_counters=lambda **k: counters)
    sampler = Sampler(fake)
    first = sampler.sample()
    assert first['cpu']['percent'] is None and first['network']['rx_bytes_s'] is None
    assert first['gpu'] is None and first['memory']['used'] == 40
    sampler.previous_time = time.monotonic() - 2
    counters['eth0'].bytes_recv += 2000
    counters['lo'].bytes_recv += 1000000
    second = sampler.sample()
    assert second['cpu']['percent'] == 20
    assert second['network']['rx_bytes_s'] == pytest.approx(1000, rel=0.01)


def test_incomplete_metric_payload_rejected():
    from backend.telemetry import validate_sample
    with pytest.raises(ValueError, match='Incomplete'):
        validate_sample({'sampled_at': time.time(), 'cpu': {}})
    with pytest.raises(ValueError, match='Invalid GPU'):
        validate_sample({'gpu': {'name': None}})
