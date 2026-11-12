import asyncio
import json
import time

import pytest
from httpx import ASGITransport, AsyncClient

from backend.app import create_app
from tests.fake_colab import PROFILES, FakeColab, template
from tests.fake_llm import FakeLLM
from tests.test_lifecycle import eventually


@pytest.fixture
async def rig(tmp_path):
    video = FakeColab()
    fake = FakeLLM(active=video.active)
    app = create_app(tmp_path, driver=video, llm_driver=fake, account_profiles=PROFILES)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as client:
            state = (await client.get('/api/state')).json()
            client.headers['x-workbench-token'] = state['token']
            yield app.state.llm, fake, client, app.state.manager


async def start(llm, account='colab2'):
    llm.request_start(account)
    await eventually(lambda: llm.machine(account)['phase'] in {'ready', 'error'})
    assert llm.machine(account)['phase'] == 'ready', llm.machine(account)['message']


def auth(llm):
    return {'Authorization': f'Bearer {llm.key()}'}


async def test_key_protection_offline_and_proxy_allowlist(rig):
    llm, fake, client, _ = rig
    assert (await client.post('/api/llm/key', headers={'x-workbench-token': 'wrong'})).status_code == 403
    key = (await client.post('/api/llm/key')).json()['key']
    assert key and key not in (await client.get('/api/state')).text
    url = '/llm/colab2/v1/models'
    assert (await client.get(url)).status_code == 401
    assert (await client.get(url, headers={'Authorization': 'Bearer wrong'})).status_code == 401
    assert (await client.get(url, headers=auth(llm))).status_code == 503
    assert not fake.calls
    await start(llm)
    assert (await client.get(url, headers=auth(llm))).json()['data'][0]['id'] == 'workbench-llm'
    assert (await client.get('/llm/colab2/v1/private', headers=auth(llm))).status_code == 404
    assert (await client.get('/llm/colab99/v1/models', headers=auth(llm))).status_code == 404
    assert (await client.post('/llm/colab2/v1/models', headers=auth(llm))).status_code == 404
    assert (await client.get(url, headers={**auth(llm), 'origin': 'https://evil.test'})).status_code == 403
    assert (await client.get(url, headers={**auth(llm), 'host': 'evil.test'})).status_code == 400


async def test_config_is_explicit_validated_and_locked_until_release(rig):
    llm, fake, client, _ = rig
    config = {**llm.machine('colab2')['config'], 'preset_id': 'flash-iq3', 'context_tokens': 131072}
    assert (await client.put('/api/llm/colab2/config', json={**config, 'preset_id': 'arbitrary-code'})).status_code == 400
    assert (await client.put('/api/llm/colab2/config', json={**config, 'context_tokens': 999})).status_code == 422
    assert (await client.put('/api/llm/colab2/config', json=config)).status_code == 200
    assert not fake.calls
    await start(llm)
    changed = {**config, 'context_tokens': 32768}
    assert (await client.put('/api/llm/colab2/config', json=changed)).status_code == 400
    assert (await client.put('/api/llm/colab2/config', json=config)).status_code == 200
    llm.request_stop('colab2')
    await eventually(lambda: llm.machine('colab2')['phase'] == 'stopped')
    assert (await client.put('/api/llm/colab2/config', json=changed)).status_code == 200


async def test_external_sessions_never_adopted_or_stopped_and_two_services_independent(rig):
    llm, fake, client, _ = rig
    llm.request_start('colab1')
    await eventually(lambda: llm.machine('colab1')['phase'] == 'error')
    assert not llm.machine('colab1')['session']
    assert not any(c[1] in {'create', 'exec', 'stop', 'tunnel'} for c in fake.calls)
    await start(llm, 'colab2')
    await start(llm, 'colab3')
    llm.request_start('colab2')
    for account in ('colab2', 'colab3'):
        result = await client.post(f'/llm/{account}/v1/chat/completions', headers=auth(llm),
                                   json={'model': 'workbench-llm', 'messages': []})
        assert result.json()['choices'][0]['message']['content'] == account
    assert len([c for c in fake.calls if c[1] == 'create']) == 2
    llm.request_stop('colab2')
    await eventually(lambda: llm.machine('colab2')['phase'] == 'stopped')
    assert llm.machine('colab3')['phase'] == 'ready'
    assert fake.active['colab1'][0]['name'] == 'unrelated-important-session'


async def test_stop_during_allocation_reconciles_and_frees_only_owned_vm(rig):
    llm, fake, _, _ = rig
    fake.create_gate = asyncio.Event()
    llm.request_start('colab2')
    await eventually(lambda: any(c[1] == 'create' for c in fake.calls))
    llm.request_stop('colab2')
    llm.request_stop('colab2')
    fake.create_gate.set()
    await eventually(lambda: llm.machine('colab2')['phase'] == 'stopped')
    assert not fake.active['colab2']
    assert not any(c[1] == 'exec' for c in fake.calls)
    assert len([c for c in fake.calls if c[1] == 'stop']) == 1


async def test_failure_keeps_session_for_release_and_refuses_wrong_ownership(rig):
    llm, fake, _, _ = rig
    fake.fail_deploy = True
    llm.request_start('colab2')
    await eventually(lambda: llm.machine('colab2')['phase'] == 'error')
    session = llm.machine('colab2')['session']
    assert session and 'insufficient disk' in llm.machine('colab2')['message']
    fake.fail_stop = True
    llm.request_stop('colab2')
    await eventually(lambda: llm.machine('colab2')['message'].startswith('关机未确认'))
    assert llm.machine('colab2')['session'] == session
    fake.fail_stop = False
    llm.request_stop('colab2')
    await eventually(lambda: llm.machine('colab2')['phase'] == 'stopped')
    llm.machine('colab1', session='unrelated-important-session', phase='disconnected')
    llm.request_start('colab1')
    await eventually(lambda: llm.machine('colab1')['phase'] == 'error')
    llm.request_stop('colab1')
    await eventually(lambda: llm.machine('colab1')['message'].startswith('关机未确认'))
    assert not any(c[0] == 'colab1' and c[1] in {'stop', 'tunnel', 'exec'} for c in fake.calls)
    llm.machine('colab1', session=None, phase='stopped')


async def test_stream_holds_single_slot_blocks_stop_idle_and_never_forwards_key(rig):
    llm, fake, client, _ = rig
    await start(llm)
    fake.generation_gate = asyncio.Event()
    task = asyncio.create_task(client.post('/llm/colab2/v1/chat/completions', headers=auth(llm),
                                          json={'model': 'workbench-llm', 'messages': [], 'stream': True}))
    await fake.generation_started.wait()
    await eventually(lambda: llm.active['colab2'] == 1)
    assert 'Authorization' not in fake.requests[0][2]
    assert llm.key() not in str(fake.requests)
    assert (await client.post('/api/llm/colab2/stop')).status_code == 400
    conflict = await client.post('/llm/colab2/v1/chat/completions', headers=auth(llm), json={'model': 'workbench-llm'})
    assert conflict.status_code == 409
    llm.machine('colab2', last_activity=time.time() - 9999)
    await llm.idle_sweep()
    assert 'colab2' not in llm.stopping
    fake.generation_gate.set()
    response = await task
    assert response.status_code == 200 and 'text/event-stream' in response.headers['content-type']
    assert '来自 colab2' in response.text and '[DONE]' in response.text
    assert llm.active['colab2'] == 0
    assert llm.machine('colab2')['last_activity'] > time.time() - 2
    llm.machine('colab2', last_activity=time.time() - 9999)
    await llm.idle_sweep()
    await eventually(lambda: llm.machine('colab2')['phase'] == 'stopped')


async def test_proxy_validation_upstream_errors_and_cancellation_cleanup(rig):
    llm, fake, client, _ = rig
    await start(llm)
    url = '/llm/colab2/v1/chat/completions'
    for body in ({'model': 'other'}, [], 'invalid-json'):
        result = await client.post(url, headers=auth(llm), content=body if isinstance(body, str) else json.dumps(body))
        assert result.status_code == 400
    response = await client.post(url, headers=auth(llm), json={'model': 'workbench-llm', 'max_tokens': -1})
    assert response.status_code == 400 and response.json()['error']['message'] == 'invalid max_tokens'
    assert llm.active['colab2'] == 0
    fake.generation_started.clear()
    fake.generation_gate = asyncio.Event()
    task = asyncio.create_task(client.post(url, headers=auth(llm), json={'model': 'workbench-llm', 'stream': True}))
    await fake.generation_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await eventually(lambda: llm.active['colab2'] == 0)
    fake.generation_gate.set()
    llm.log('colab2', 'Bearer ' + llm.key())
    assert llm.key() not in (await client.get('/api/llm/colab2/logs')).text


async def test_video_llm_slot_reservation_is_atomic_both_directions(rig):
    llm, fake, client, video = rig
    fake.create_gate = asyncio.Event()
    llm.request_start('colab2')
    assert (await client.post('/api/accounts/colab2/start')).status_code == 400
    with pytest.raises(ValueError, match='LLM'):
        video.submit('colab2', template(), 'blocked', False)
    fake.create_gate.set()
    await eventually(lambda: llm.machine('colab2')['phase'] == 'ready')
    llm.request_stop('colab2')
    await eventually(lambda: llm.machine('colab2')['phase'] == 'stopped')
    video.request_start('colab2')
    assert (await client.post('/api/llm/colab2/start')).status_code == 400


async def test_restart_retains_endpoint_key_and_session_but_never_autostarts(tmp_path):
    video, fake = FakeColab(), FakeLLM()
    app = create_app(tmp_path, driver=video, llm_driver=fake, account_profiles=PROFILES)
    async with app.router.lifespan_context(app):
        llm = app.state.llm
        await start(llm)
        key, session = llm.key(), llm.machine('colab2')['session']
        app.state.store.patch('settings', 'main', stop_on_exit=False)
    fake = FakeLLM(active=fake.active)
    app = create_app(tmp_path, driver=FakeColab(), llm_driver=fake, account_profiles=PROFILES)
    async with app.router.lifespan_context(app):
        llm = app.state.llm
        assert llm.key() == key and llm.machine('colab2')['session'] == session
        assert llm.machine('colab2')['phase'] == 'disconnected'
        assert llm.public('colab2')['endpoint_path'] == '/llm/colab2/v1'
        assert not fake.calls
        app.state.store.patch('settings', 'main', stop_on_exit=True)


async def test_shutdown_inflight_allocation_does_not_orphan_vm(tmp_path):
    fake = FakeLLM()
    app = create_app(tmp_path, driver=FakeColab(), llm_driver=fake, account_profiles=PROFILES)
    context = app.router.lifespan_context(app)
    await context.__aenter__()
    fake.create_gate = asyncio.Event()
    app.state.llm.request_start('colab2')
    await eventually(lambda: any(c[1] == 'create' for c in fake.calls))
    closing = asyncio.create_task(context.__aexit__(None, None, None))
    await asyncio.sleep(.05)
    fake.create_gate.set()
    await asyncio.wait_for(closing, timeout=5)
    assert not fake.active['colab2']


@pytest.mark.parametrize('ids', [[], ['colab12'], ['colab7', 'colab12']])
async def test_service_discovery_supports_zero_one_and_nonconsecutive_accounts(tmp_path, ids):
    profiles = [{**PROFILES[0], 'id': account} for account in ids]
    fake = FakeLLM(active={account: [] for account in ids})
    app = create_app(tmp_path, driver=FakeColab(), llm_driver=fake, account_profiles=profiles)
    async with app.router.lifespan_context(app):
        llm = app.state.llm
        assert [llm.public(account)['endpoint_path'] for account in ids] == [f'/llm/{account}/v1' for account in ids]
        if ids:
            await start(llm, ids[-1])
            session = llm.machine(ids[-1])['session']
            assert llm.owned(ids[-1], session)
            assert not llm.owned('colab1', session)
        else:
            with pytest.raises(ValueError, match='未知账号'):
                llm.request_start('colab1')
