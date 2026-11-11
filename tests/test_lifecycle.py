import asyncio
import time

import pytest

from backend.manager import Manager
from backend.store import Store
from tests.fake_colab import PROFILES, FakeColab, template


async def eventually(condition, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        await asyncio.sleep(0.03)
    raise AssertionError("Condition did not become true")


@pytest.fixture
async def rig(tmp_path):
    store, fake = Store(tmp_path), FakeColab()
    manager = Manager(store, fake, PROFILES)
    await manager.open()
    yield manager, fake, store
    await manager.close()
    store.close()


async def test_generate_download_then_release_owned_machine_only(rig):
    manager, fake, store = rig
    job = manager.submit("colab2", template(), "Fox", True)
    await eventually(lambda: manager.job(job['id'])['status'] == 'completed')
    await eventually(lambda: manager.machine('colab2')['phase'] == 'stopped')
    result = manager.job(job['id'])
    output = store.directory / 'outputs' / job['id'] / result['outputs'][0]['filename']
    assert output.stat().st_size > 100_000
    assert (output.parent / 'workflow-api.json').is_file()
    assert fake.active['colab1'][0]['name'] == 'unrelated-important-session'
    assert len(fake.prompt_posts) == 1
    assert fake.downloaded
    assert all(call[0] == 'colab2' for call in fake.calls)


async def test_start_is_idempotent_and_accounts_are_independent(rig):
    manager, fake, _ = rig
    manager.request_start('colab2')
    manager.request_start('colab2')
    manager.request_start('colab3')
    await eventually(lambda: all(manager.machine(a)['phase'] == 'ready' for a in ('colab2', 'colab3')))
    assert len([c for c in fake.calls if c[1] == 'create']) == 2
    manager.request_stop('colab2')
    await eventually(lambda: manager.machine('colab2')['phase'] == 'stopped')
    assert manager.machine('colab3')['phase'] == 'ready'
    assert len(fake.active['colab3']) == 1


async def test_stop_during_allocation_waits_and_releases(rig):
    manager, fake, _ = rig
    fake.create_gate = asyncio.Event()
    manager.request_start('colab2')
    await eventually(lambda: any(c[1] == 'create' for c in fake.calls))
    manager.request_stop('colab2')
    fake.create_gate.set()
    await eventually(lambda: manager.machine('colab2')['phase'] == 'stopped')
    assert not fake.active['colab2']
    assert not any(c[1] == 'exec' for c in fake.calls)


async def test_refuses_unowned_session_and_reports_failed_shutdown(rig):
    manager, fake, _ = rig
    manager.machine('colab1', session='unrelated-important-session', phase='ready')
    manager.request_stop('colab1')
    await eventually(lambda: manager.machine('colab1')['phase'] == 'error')
    assert not any(c[1] == 'stop' for c in fake.calls)
    manager.machine('colab1', session=None, phase='stopped')
    manager.request_start('colab2')
    await eventually(lambda: manager.machine('colab2')['phase'] == 'ready')
    fake.fail_stop = True
    manager.request_stop('colab2')
    await eventually(lambda: manager.machine('colab2')['phase'] == 'error')
    assert manager.machine('colab2')['session']
    assert len(fake.active['colab2']) == 1
    fake.fail_stop = False


async def test_cancel_targets_prompt_and_stop_rejects_active_queue(rig):
    manager, fake, _ = rig
    fake.delay = 30
    job = manager.submit('colab2', template(), 'Cancel', False)
    await eventually(lambda: bool(fake.prompt_posts))
    with pytest.raises(ValueError, match='未完成任务'):
        manager.request_stop('colab2')
    await manager.cancel(job['id'])
    assert manager.job(job['id'])['status'] == 'cancelled'
    assert ('colab2', 'cancel', manager.job(job['id'])['prompt_id']) in fake.calls
    manager.request_stop('colab2')
    await eventually(lambda: manager.machine('colab2')['phase'] == 'stopped')


async def test_recovery_reads_history_without_resubmitting(rig):
    manager, fake, store = rig
    job = manager.submit('colab2', template(), 'Recover', False)
    await eventually(lambda: manager.job(job['id'])['status'] == 'completed')
    posts = len(fake.prompt_posts)
    manager.job(job['id'], status='recovering', outputs=[])
    manager.resume(job['id'])
    await eventually(lambda: manager.job(job['id'])['status'] == 'completed')
    assert len(fake.prompt_posts) == posts


async def test_unknown_old_session_does_not_rerun_submitted_job(rig):
    manager, fake, store = rig
    store.put('jobs', 'lost', {'id': 'lost', 'account': 'colab2', 'status': 'recovering', 'prompt_id': 'old-prompt', 'created_at': time.time()})
    manager.machine('colab2', phase='disconnected', session='wb-old-missing')
    manager.request_start('colab2')
    await eventually(lambda: manager.machine('colab2')['phase'] == 'ready')
    assert manager.job('lost')['status'] == 'failed'
    assert not fake.prompt_posts


async def test_stop_after_job_prevents_adding_work_behind_it(rig):
    manager, fake, _ = rig
    fake.delay = 30
    manager.submit('colab2', template(), 'Final', True)
    with pytest.raises(ValueError, match='完成后关机'):
        manager.submit('colab2', template(), 'Too late', False)


async def test_shutdown_during_worker_allocation_does_not_orphan_vm(tmp_path):
    store, fake = Store(tmp_path), FakeColab()
    manager = Manager(store, fake, PROFILES)
    fake.create_gate = asyncio.Event()
    await manager.open()
    manager.submit('colab2', template(), 'In flight', False)
    await eventually(lambda: any(c[1] == 'create' for c in fake.calls))
    closing = asyncio.create_task(manager.close())
    await asyncio.sleep(0.1)
    fake.create_gate.set()
    await asyncio.wait_for(closing, timeout=5)
    assert not fake.active['colab2']
    assert any(c[1] == 'stop' for c in fake.calls)
    store.close()


async def test_manual_stop_detects_native_editor_tasks(rig):
    manager, fake, _ = rig
    manager.request_start('colab2')
    await eventually(lambda: manager.machine('colab2')['phase'] == 'ready')
    fake.native_busy = True
    manager.request_stop('colab2')
    await eventually(lambda: '仍有任务' in manager.machine('colab2')['message'])
    assert manager.machine('colab2')['phase'] == 'ready'
    assert not any(c[1] == 'stop' for c in fake.calls)
    assert '仍有任务' in manager.machine('colab2')['message']
    fake.native_busy = False


async def test_idle_shutdown_waits_for_native_queue_then_stops(rig):
    manager, fake, _ = rig
    manager.request_start('colab2')
    await eventually(lambda: manager.machine('colab2')['phase'] == 'ready')
    manager.machine('colab2', last_activity=time.time() - 3600)
    fake.native_busy = True
    await manager.idle_sweep()
    assert manager.machine('colab2')['phase'] == 'ready'
    assert not any(c[1] == 'stop' for c in fake.calls)
    fake.native_busy = False
    manager.machine('colab2', last_activity=time.time() - 3600)
    await manager.idle_sweep()
    await eventually(lambda: manager.machine('colab2')['phase'] == 'stopped')
