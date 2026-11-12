import asyncio
import json
import time

import pytest
from httpx import ASGITransport, AsyncClient

from backend.app import create_app
from backend.llm_presets import default_config
from backend.store import Store
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


def create(llm, account='colab2', preset='qwen27-fp8'):
    return llm.create(account, {**default_config(), 'preset_id': preset})['id']


async def start(llm, key):
    llm.request_start(key)
    await eventually(lambda: llm.machine(key)['phase'] in {'ready', 'error'})
    assert llm.machine(key)['phase'] == 'ready', llm.machine(key)['message']


def auth(llm):
    return {'Authorization': f'Bearer {llm.key()}'}


def payload(llm, key, **extra):
    return {'model': llm.model_name(key), 'messages': [], **extra}


async def test_key_protection_offline_and_proxy_allowlist(rig):
    llm, fake, client, _ = rig
    key = create(llm)
    assert (await client.post('/api/llm/key', headers={'x-workbench-token': 'wrong'})).status_code == 403
    api_key = (await client.post('/api/llm/key')).json()['key']
    assert api_key and api_key not in (await client.get('/api/state')).text
    url = f'/llm/{key}/v1/models'
    assert (await client.get(url)).status_code == 401
    assert (await client.get(url, headers={'Authorization': 'Bearer wrong'})).status_code == 401
    assert (await client.get(url, headers=auth(llm))).status_code == 503
    assert not fake.calls
    await start(llm, key)
    assert (await client.get(url, headers=auth(llm))).json()['data'][0]['id'] == llm.model_name(key)
    assert llm.model_name(key) == 'Qwen/Qwen3.8-27B-FP8'
    assert (await client.get(f'/llm/{key}/v1/private', headers=auth(llm))).status_code == 404
    assert (await client.get('/llm/absent/v1/models', headers=auth(llm))).status_code == 404
    assert (await client.post(url, headers=auth(llm))).status_code == 404
    assert (await client.get(url, headers={**auth(llm), 'origin': 'https://evil.test'})).status_code == 403
    assert (await client.get(url, headers={**auth(llm), 'host': 'evil.test'})).status_code == 400


async def test_create_config_validates_without_cloud_calls_and_model_identity_follows_recipe(rig):
    llm, fake, client, _ = rig
    body = {'account': 'colab2', 'config': {**default_config(), 'preset_id': 'flash-iq3', 'context_tokens': 131072}}
    assert (await client.post('/api/llm/instances', json={**body, 'account': 'other'})).status_code == 400
    assert (await client.post('/api/llm/instances', json={**body, 'config': {**body['config'], 'preset_id': 'arbitrary-code'}})).status_code == 400
    assert (await client.post('/api/llm/instances', json={**body, 'config': {**body['config'], 'context_tokens': 999}})).status_code == 422
    created = await client.post('/api/llm/instances', json=body)
    assert created.status_code == 201 and not fake.calls
    item = created.json()
    key, path = item['id'], item['endpoint_path']
    assert 'Flash-Next' in item['model_name']
    changed = {'account': 'colab3', 'config': {**default_config(), 'preset_id': 'qwen27-rana'}}
    result = (await client.put(f'/api/llm/{key}/config', json=changed)).json()
    assert result['account'] == 'colab3' and 'RANA' in result['model_name']
    assert result['endpoint_path'] == path
    await start(llm, key)
    assert (await client.put(f'/api/llm/{key}/config', json=body)).status_code == 400
    assert (await client.put(f'/api/llm/{key}/config', json=changed)).status_code == 200
    assert (await client.delete(f'/api/llm/{key}')).status_code == 400
    llm.request_stop(key)
    await eventually(lambda: llm.machine(key)['phase'] == 'stopped')
    assert (await client.delete(f'/api/llm/{key}')).status_code == 200
    assert (await client.get(f'/llm/{key}/v1/models', headers=auth(llm))).status_code == 404


async def test_same_account_multiple_instances_have_independent_models_requests_tunnels_and_stops(rig):
    llm, fake, client, _ = rig
    first, second = create(llm), create(llm, preset='flash-iq3')
    llm.request_start(first)
    llm.request_start(second)
    llm.request_start(first)
    await eventually(lambda: all(llm.machine(k)['phase'] == 'ready' for k in (first, second)))
    assert len(fake.active['colab2']) == 2
    assert len([c for c in fake.calls if c[1] == 'create']) == 2
    assert llm.machine(first)['port'] != llm.machine(second)['port']
    assert not llm.owned(first, llm.machine(second)['session'])
    for key in (first, second):
        result = await client.post(f'/llm/{key}/v1/chat/completions', headers=auth(llm), json=payload(llm, key))
        assert result.json()['choices'][0]['message']['content'] == key
        assert fake.requests[-1][3] == llm.machine(key)['session']
        metrics = (await client.get(f'/api/llm/{key}/metrics')).json()
        assert metrics['account'] == key and metrics['session'] == llm.machine(key)['session']
    llm.request_stop(first)
    await eventually(lambda: llm.machine(first)['phase'] == 'stopped')
    assert first not in fake.ports and second in fake.ports
    assert len(fake.active['colab2']) == 1 and llm.machine(second)['phase'] == 'ready'
    assert (await client.get(f'/llm/{second}/v1/models', headers=auth(llm))).status_code == 200
    assert llm.public(first)['endpoint_path'] != llm.public(second)['endpoint_path']


async def test_external_and_video_sessions_can_coexist_without_being_adopted_or_stopped(rig):
    llm, fake, client, video = rig
    key = create(llm, 'colab1')
    await start(llm, key)
    assert len(fake.active['colab1']) == 2
    assert fake.active['colab1'][0]['name'] == 'unrelated-important-session'
    llm.request_stop(key)
    await eventually(lambda: llm.machine(key)['phase'] == 'stopped')
    assert [s['name'] for s in fake.active['colab1']] == ['unrelated-important-session']
    video.request_start('colab2')
    await eventually(lambda: video.machine('colab2')['phase'] == 'ready')
    first = create(llm)
    await start(llm, first)
    assert len(fake.active['colab2']) == 2
    job = video.submit('colab2', template(), 'Independent video', False)
    await eventually(lambda: video.job(job['id'])['status'] == 'completed')
    assert llm.machine(first)['phase'] == 'ready'
    assert all(c[2] != 'unrelated-important-session' for c in fake.calls if c[1] in {'stop', 'exec', 'tunnel'})


async def test_actual_allocation_limit_allows_switching_account_without_touching_siblings(rig):
    llm, fake, _, _ = rig
    first = create(llm)
    await start(llm, first)
    second = create(llm)
    fake.quota_accounts.add('colab2')
    llm.request_start(second)
    await eventually(lambda: llm.machine(second)['phase'] == 'error')
    assert not llm.machine(second)['session']
    assert '更换账号' in llm.machine(second)['message']
    llm.configure(second, 'colab3', llm.machine(second)['config'])
    await start(llm, second)
    assert llm.machine(first)['phase'] == 'ready' and len(fake.active['colab2']) == 1
    assert len(fake.active['colab3']) == 1


async def test_stop_during_allocation_reconciles_and_frees_only_its_own_vm(rig):
    llm, fake, _, _ = rig
    first = create(llm)
    await start(llm, first)
    fake.create_gate = asyncio.Event()
    second = create(llm)
    llm.request_start(second)
    await eventually(lambda: len([c for c in fake.calls if c[1] == 'create']) == 2)
    assert llm.request_stop(second)['phase'] == 'stopping'
    llm.request_stop(second)
    fake.create_gate.set()
    await eventually(lambda: llm.machine(second)['phase'] == 'stopped')
    assert llm.machine(first)['phase'] == 'ready' and len(fake.active['colab2']) == 1
    assert len([c for c in fake.calls if c[1] == 'stop']) == 1


async def test_failure_keeps_session_for_release_and_refuses_sibling_or_external_ownership(rig):
    llm, fake, _, _ = rig
    key = create(llm)
    fake.fail_deploy = True
    llm.request_start(key)
    await eventually(lambda: llm.machine(key)['phase'] == 'error')
    session = llm.machine(key)['session']
    assert session and 'insufficient disk' in llm.machine(key)['message']
    fake.fail_stop = True
    llm.request_stop(key)
    await eventually(lambda: llm.machine(key)['message'].startswith('关机未确认'))
    assert llm.machine(key)['session'] == session
    fake.fail_stop = False
    previous_calls = len(fake.calls)
    other = create(llm)
    llm.machine(other, session=session, phase='disconnected')
    llm.request_start(other)
    await eventually(lambda: llm.machine(other)['phase'] == 'error')
    llm.request_stop(other)
    await eventually(lambda: llm.machine(other)['message'].startswith('关机未确认'))
    assert not any(c[1] == 'stop' and c[2] == session for c in fake.calls[previous_calls:])
    llm.machine(other, session=None, phase='stopped')
    llm.request_stop(key)
    await eventually(lambda: llm.machine(key)['phase'] == 'stopped')


async def test_stream_slots_are_per_instance_and_blocks_stop_idle_without_forwarding_key(rig):
    llm, fake, client, _ = rig
    first, second = create(llm), create(llm)
    await start(llm, first)
    await start(llm, second)
    fake.generation_gate = asyncio.Event()
    task = asyncio.create_task(client.post(f'/llm/{first}/v1/chat/completions', headers=auth(llm), json=payload(llm, first, stream=True)))
    await fake.generation_started.wait()
    await eventually(lambda: llm.active[first] == 1)
    assert 'Authorization' not in fake.requests[0][2]
    assert llm.key() not in str(fake.requests)
    assert (await client.post(f'/api/llm/{first}/stop')).status_code == 400
    assert (await client.post(f'/llm/{first}/v1/chat/completions', headers=auth(llm), json=payload(llm, first))).status_code == 409
    # A busy sibling must not block a different instance on the same account.
    other = asyncio.create_task(client.post(f'/llm/{second}/v1/chat/completions', headers=auth(llm), json=payload(llm, second)))
    await eventually(lambda: llm.active[second] == 1)
    llm.machine(first, last_activity=time.time() - 9999)
    await llm.idle_sweep()
    assert first not in llm.stopping
    fake.generation_gate.set()
    response, other_response = await asyncio.gather(task, other)
    assert response.status_code == other_response.status_code == 200
    assert first in response.text and '[DONE]' in response.text
    assert llm.active[first] == llm.active[second] == 0
    assert llm.machine(first)['last_activity'] > time.time() - 2
    llm.machine(first, last_activity=time.time() - 9999)
    await llm.idle_sweep()
    await eventually(lambda: llm.machine(first)['phase'] == 'stopped')
    assert llm.machine(second)['phase'] == 'ready'


async def test_proxy_validation_upstream_errors_and_cancellation_cleanup(rig):
    llm, fake, client, _ = rig
    key = create(llm)
    await start(llm, key)
    url = f'/llm/{key}/v1/chat/completions'
    for body in ({'model': 'workbench-llm'}, [], 'invalid-json'):
        result = await client.post(url, headers=auth(llm), content=body if isinstance(body, str) else json.dumps(body))
        assert result.status_code == 400
    response = await client.post(url, headers=auth(llm), json=payload(llm, key, max_tokens=-1))
    assert response.status_code == 400 and response.json()['error']['message'] == 'invalid max_tokens'
    assert llm.active[key] == 0
    fake.generation_started.clear()
    fake.generation_gate = asyncio.Event()
    task = asyncio.create_task(client.post(url, headers=auth(llm), json=payload(llm, key, stream=True)))
    await fake.generation_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await eventually(lambda: llm.active[key] == 0)
    fake.generation_gate.set()
    llm.log(key, 'Bearer ' + llm.key())
    assert llm.key() not in (await client.get(f'/api/llm/{key}/logs')).text


async def test_restart_retains_instance_endpoints_and_key_but_never_autostarts(tmp_path):
    video, fake = FakeColab(), FakeLLM()
    app = create_app(tmp_path, driver=video, llm_driver=fake, account_profiles=PROFILES)
    async with app.router.lifespan_context(app):
        llm = app.state.llm
        ids = [create(llm), create(llm)]
        for key in ids:
            await start(llm, key)
        api_key = llm.key()
        llm.rename(ids[0], '长期服务')
        sessions = [llm.machine(key)['session'] for key in ids]
        llm.log(ids[0], 'preserve this instance log')
        app.state.store.patch('settings', 'main', stop_on_exit=False)
    fake = FakeLLM(active=fake.active)
    app = create_app(tmp_path, driver=FakeColab(), llm_driver=fake, account_profiles=PROFILES)
    async with app.router.lifespan_context(app):
        llm = app.state.llm
        assert llm.key() == api_key
        assert llm.machine(ids[0])['name'] == '长期服务'
        assert [llm.machine(key)['session'] for key in ids] == sessions
        assert all(llm.machine(key)['phase'] == 'disconnected' for key in ids)
        assert [llm.public(key)['endpoint_path'] for key in ids] == [f'/llm/{key}/v1' for key in ids]
        assert not fake.calls
        assert llm.store.get('llm_logs', ids[0])[-1]['message'] == 'preserve this instance log'
        app.state.store.patch('settings', 'main', stop_on_exit=True)


async def test_shutdown_inflight_allocation_does_not_orphan_vm(tmp_path):
    fake = FakeLLM()
    app = create_app(tmp_path, driver=FakeColab(), llm_driver=fake, account_profiles=PROFILES)
    context = app.router.lifespan_context(app)
    await context.__aenter__()
    key = create(app.state.llm)
    fake.create_gate = asyncio.Event()
    app.state.llm.request_start(key)
    await eventually(lambda: any(c[1] == 'create' for c in fake.calls))
    closing = asyncio.create_task(context.__aexit__(None, None, None))
    await asyncio.sleep(.05)
    fake.create_gate.set()
    await asyncio.wait_for(closing, timeout=5)
    assert not fake.active['colab2']


@pytest.mark.parametrize('ids', [[], ['colab12'], ['colab7', 'colab12']])
async def test_services_support_zero_one_and_nonconsecutive_accounts(tmp_path, ids):
    profiles = [{**PROFILES[0], 'id': account} for account in ids]
    fake = FakeLLM(active={account: [] for account in ids})
    app = create_app(tmp_path, driver=FakeColab(), llm_driver=fake, account_profiles=profiles)
    async with app.router.lifespan_context(app):
        llm = app.state.llm
        assert llm.all() == []  # Accounts no longer imply pre-created service instances.
        if ids:
            key = create(llm, ids[-1])
            await start(llm, key)
            assert llm.owned(key, llm.machine(key)['session'])
            assert llm.machine(key)['account'] == ids[-1]
        else:
            with pytest.raises(ValueError, match='未知账号'):
                create(llm, 'colab1')


async def test_migrates_live_legacy_sessions_configs_and_logs_without_touching_cloud(tmp_path):
    store = Store(tmp_path)
    store.put('meta', 'install', {'id': 'test-install'})
    session = 'wbllm-test-install-2-legacy'
    store.put('llm', 'colab2', {'account': 'colab2', 'session': session, 'phase': 'ready', 'config': default_config(), 'port': 1})
    store.put('llm_logs', 'colab2', [{'time': 1, 'message': 'legacy log'}])
    store.put('llm', 'colab3', {'account': 'colab3', 'session': None, 'phase': 'stopped', 'config': default_config()})
    store.close()
    fake = FakeLLM()
    fake.active['colab2'] = [{'name': session}]
    fake.ready.add(session)
    fake.models[session] = 'workbench-llm'
    app = create_app(tmp_path, driver=FakeColab(), llm_driver=fake, account_profiles=PROFILES)
    async with app.router.lifespan_context(app):
        llm = app.state.llm
        assert len(llm.all()) == 1 and not fake.calls
        key = llm.all()[0]['id']
        assert llm.resolve('colab2') == key
        assert llm.machine(key)['session'] == session
        assert llm.machine(key)['phase'] == 'disconnected'
        assert llm.machine(key)['name']
        assert llm.store.get('llm_logs', key)[0]['message'] == 'legacy log'
        assert llm.model_name(key) == 'workbench-llm'
        await start(llm, key)
        assert not any(c[1] in {'create', 'exec'} for c in fake.calls)
        llm.request_stop(key)
        await eventually(lambda: llm.machine(key)['phase'] == 'stopped')
        assert llm.model_name(key) == 'Qwen/Qwen3.8-27B-FP8'


async def test_real_colab_adapter_isolates_ssh_tunnels_on_the_same_account(tmp_path, monkeypatch):
    import sys

    from backend.colab import Colab

    launcher, identity = tmp_path / 'launcher', tmp_path / 'identity'
    launcher.write_text('#!' + sys.executable + '\n')
    identity.touch()
    profiles = [{**PROFILES[0], 'id': 'colab2', 'launcher': str(launcher)}]
    children, killed = [], []

    class Child:
        returncode = None

        def __init__(self, args):
            self.args, self.pid = args, 100 + len(children)

        async def wait(self):
            self.returncode = 0

    async def spawn(*args, **kwargs):
        child = Child(args)
        children.append(child)
        return child

    async def no_delay(*args):
        pass

    monkeypatch.setattr('backend.colab.asyncio.create_subprocess_exec', spawn)
    monkeypatch.setattr('backend.colab.asyncio.sleep', no_delay)
    monkeypatch.setattr('backend.colab.os.killpg', lambda pid, _: killed.append(pid))
    driver = Colab(profiles, tmp_path)
    await driver.tunnel('colab2', 'first', str(identity), service_port=8000, tunnel_key='instance-first')
    await driver.tunnel('colab2', 'second', str(identity), service_port=8000, tunnel_key='instance-second')
    assert len(driver.tunnels) == len(driver.telemetry_ports) == 2 and not killed
    await driver.close_tunnel('instance-first')
    assert killed == [children[0].pid]
    assert driver.tunnels['instance-second'] is children[1]
    assert children[1].returncode is None
    await driver.close()
    assert killed == [child.pid for child in children]


async def test_instance_names_are_separate_from_model_identity_and_can_change_during_generation(rig):
    llm, fake, client, _ = rig
    result = await client.post('/api/llm/instances', json={'account': 'colab2', 'config': default_config(), 'name': '  日常助手  '})
    assert result.status_code == 201 and result.json()['name'] == '日常助手'
    key = result.json()['id']
    first_auto, second_auto = create(llm), create(llm)
    assert llm.machine(first_auto)['name'] != llm.machine(second_auto)['name']
    llm.configure(key, 'colab2', {**default_config(), 'preset_id': 'qwen27-rana'})
    assert llm.machine(key)['name'] == '日常助手'
    llm.configure(first_auto, 'colab2', {**default_config(), 'preset_id': 'flash-iq3'})
    assert 'Flash-Next' in llm.machine(first_auto)['name']
    await start(llm, key)
    identity = {k: llm.public(key)[k] for k in ('id', 'account', 'session', 'model_name', 'endpoint_path', 'config')}
    fake.generation_gate = asyncio.Event()
    generation = asyncio.create_task(client.post(f'/llm/{key}/v1/chat/completions', headers=auth(llm), json=payload(llm, key, stream=True)))
    await fake.generation_started.wait()
    calls = len(fake.calls)
    response = await client.patch(f'/api/llm/{key}', json={'name': '  长文分析  '})
    assert response.status_code == 200 and response.json()['name'] == '长文分析'
    assert {k: response.json()[k] for k in identity} == identity
    assert llm.active[key] == 1 and llm.machine(key)['phase'] == 'ready'
    assert len(fake.calls) == calls
    for invalid in ['', '   ', 'line\nbreak', 'x' * 81]:
        assert (await client.patch(f'/api/llm/{key}', json={'name': invalid})).status_code in {400, 422}
    fake.generation_gate.set()
    assert (await generation).status_code == 200
    assert llm.machine(key)['name'] == '长文分析'
    assert llm.machine(second_auto)['name'] != '长文分析'
