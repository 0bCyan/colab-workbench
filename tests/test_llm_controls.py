import json
import threading

import pytest

from backend import deploy_llm_remote
from backend.deploy_llm_remote import configured_profile
from backend.llm import LLM
from backend.llm_presets import default_config, request_defaults
from backend.store import Store
from tests import test_llm as helpers
from tests.fake_colab import PROFILES
from tests.fake_llm import FakeLLM
from tests.test_llm import auth, create, payload, start


@pytest.fixture
async def controls_rig(tmp_path):
    async for values in helpers.rig.__wrapped__(tmp_path):
        yield values


async def test_custom_deployment_config_is_validated_and_runtime_defaults_remain_live(controls_rig):
    llm, fake, client, _ = controls_rig
    config = {**default_config(), 'context_tokens': 65535, 'cpu_threads': 17,
              'cache_enabled': False, 'cache_mib': 2048, 'download_workers': 128}
    response = await client.post('/api/llm/instances', json={'account': 'colab2', 'config': config})
    assert response.status_code == 201
    key = response.json()['id']
    assert response.json()['config'] == config
    for invalid in ({'context_tokens': 262145}, {'cpu_threads': 49}, {'cache_mib': 8193},
                    {'download_workers': 257}, {'context_tokens': True}):
        response = await client.put(f'/api/llm/{key}/config', json={'account': 'colab2', 'config': {**config, **invalid}})
        assert response.status_code == 422
    await start(llm, key)
    calls = list(fake.calls)
    session = llm.machine(key)['session']
    changes = {'thinking': False, 'reasoning_effort': 'medium', 'max_tokens': 8192,
               'reasoning_budget_tokens': 2048, 'tools_enabled': True, 'parallel_tool_calls': True}
    response = await client.put(f'/api/llm/{key}/defaults', json=changes)
    assert response.status_code == 200 and response.json()['request_defaults'] == {**request_defaults(config['context_tokens']), **changes}
    assert llm.machine(key)['session'] == session and llm.machine(key)['phase'] == 'ready'
    assert fake.calls == calls
    response = await client.post(f'/llm/{key}/v1/chat/completions', headers=auth(llm), json=payload(llm, key))
    assert response.status_code == 200
    sent = fake.requests[-1][1]
    assert sent['max_tokens'] == 8192 and sent['reasoning_effort'] == 'none' and 'reasoning_budget_tokens' not in sent
    explicit = payload(llm, key, max_completion_tokens=123, reasoning_effort='low', reasoning_budget_tokens=500,
                       parallel_tool_calls=False, tools=[{'type': 'function', 'function': {'name': 'lookup'}}], tool_choice='required')
    response = await client.post(f'/llm/{key}/v1/chat/completions', headers=auth(llm), json=explicit)
    assert response.status_code == 200
    assert fake.requests[-1][1] == explicit
    explicit = payload(llm, key, chat_template_kwargs={'enable_thinking': False}, thinking_budget_tokens=11)
    await client.post(f'/llm/{key}/v1/chat/completions', headers=auth(llm), json=explicit)
    assert 'reasoning_effort' not in fake.requests[-1][1]
    assert 'reasoning_budget_tokens' not in fake.requests[-1][1]
    for invalid in ({'max_tokens': 65535}, {'reasoning_budget_tokens': 8193}, {'thinking': None},
                    {'reasoning_effort': 'invented'}, {'tools_enabled': 1}, {'command': 'anything'}):
        response = await client.put(f'/api/llm/{key}/defaults', json=invalid)
        assert response.status_code in {400, 422}


async def test_tool_envelopes_survive_openai_responses_and_anthropic_routes(controls_rig):
    llm, fake, client, _ = controls_rig
    key = create(llm)
    await start(llm, key)
    tools = [{'type': 'function', 'function': {'name': 'lookup', 'parameters': {'type': 'object',
              'properties': {'city': {'type': 'string'}}, 'required': ['city']}}}]
    history = [{'role': 'user', 'content': '查一下杭州'},
               {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'call_old', 'type': 'function',
                'function': {'name': 'lookup', 'arguments': '{"city":"杭州"}'}}]},
               {'role': 'tool', 'tool_call_id': 'call_old', 'content': '{"weather":"晴"}'}]
    for stream in (False, True):
        body = payload(llm, key, messages=history, tools=tools, tool_choice='auto', parallel_tool_calls=True, stream=stream)
        response = await client.post(f'/llm/{key}/v1/chat/completions', headers=auth(llm), json=body)
        assert response.status_code == 200 and 'tool_calls' in response.text
        assert fake.requests[-1][1]['messages'] == history and fake.requests[-1][1]['tools'] == tools
        if not stream:
            call = response.json()['choices'][0]['message']['tool_calls'][0]
            assert json.loads(call['function']['arguments']) == {'city': '杭州'}
    body = payload(llm, key, input=[{'type': 'function_call_output', 'call_id': 'call_old', 'output': '晴'}], tools=tools)
    response = await client.post(f'/llm/{key}/v1/responses', headers=auth(llm), json=body)
    assert response.status_code == 200 and response.json()['output'][0]['type'] == 'function_call'
    assert fake.requests[-1][1]['input'] == body['input']
    assert fake.requests[-1][1]['max_output_tokens'] == 4096
    body = payload(llm, key, tools=[{'name': 'lookup', 'input_schema': {'type': 'object'}}],
                   messages=[{'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'old', 'content': '晴'}]}])
    response = await client.post(f'/llm/{key}/v1/messages',
                                 headers={'x-api-key': llm.key(), 'anthropic-version': '2023-06-01'}, json=body)
    assert response.status_code == 200 and response.json()['content'][0]['type'] == 'tool_use'
    assert fake.requests[-1][1]['messages'] == body['messages']
    headers = {name.lower(): value for name, value in fake.requests[-1][2].items()}
    assert headers['anthropic-version'] == '2023-06-01'
    assert 'x-api-key' not in headers and 'authorization' not in headers
    assert (await client.post(f'/llm/{key}/v1/messages', headers={'x-api-key': 'wrong'}, json=body)).status_code == 401
    response = await client.post(f'/llm/{key}/v1/messages/count_tokens', headers={'x-api-key': llm.key()}, json=body)
    assert response.status_code == 200 and response.json() == {'input_tokens': 123}
    assert 'max_tokens' not in fake.requests[-1][1]


async def test_runtime_defaults_persist_and_context_reduction_clamps_saved_limits(tmp_path):
    store = Store(tmp_path)
    store.put('settings', 'main', {'stop_on_exit': False})
    llm = LLM(store, FakeLLM(), PROFILES)
    key = create(llm)
    llm.configure_defaults(key, {'max_tokens': 60000, 'reasoning_budget_tokens': 30000, 'reasoning_effort': 'low'})
    llm.configure(key, 'colab2', {**default_config(), 'context_tokens': 32768})
    await llm.close()
    reloaded = LLM(store, FakeLLM(), PROFILES)
    defaults = reloaded.public(key)['request_defaults']
    assert defaults['max_tokens'] == 32767 and defaults['reasoning_budget_tokens'] == 30000
    assert defaults['reasoning_effort'] == 'low'
    assert set(defaults) == set(request_defaults())
    await reloaded.close()
    store.close()


async def test_official_qwen_thinking_controls_map_to_native_and_defaults_do_not_conflict(controls_rig):
    llm, fake, client, _ = controls_rig
    key = create(llm)
    await start(llm, key)
    url = f'/llm/{key}/v1/chat/completions'
    for requested, expected in [('low', 'low'), ('medium', 'medium'), ('xhigh', 'xhigh'),
                                ('high', 'xhigh'), ('max', 'xhigh'), ('minimal', 'low'), ('none', 'none')]:
        response = await client.post(url, headers=auth(llm), json=payload(llm, key, reasoning_effort=requested))
        assert response.status_code == 200
        sent = fake.requests[-1][1]
        assert sent['reasoning_effort'] == expected
        assert 'reasoning_budget_tokens' not in sent
    response = await client.put(f'/api/llm/{key}/defaults', json={'reasoning_effort': 'high'})
    assert response.json()['request_defaults']['reasoning_effort'] == 'xhigh'
    for budget, effort in [(0, 'low'), (4096, 'low'), (4097, 'medium'), (16384, 'medium'), (16385, 'xhigh')]:
        response = await client.post(url, headers=auth(llm), json=payload(llm, key, enable_thinking=True, thinking_budget=budget))
        assert response.status_code == 200
        sent = fake.requests[-1][1]
        assert sent['reasoning_budget_tokens'] == budget and sent['reasoning_effort'] == effort
        assert sent['chat_template_kwargs']['enable_thinking'] is True
        assert 'thinking_budget' not in sent and 'enable_thinking' not in sent
    for fields in ({'thinking_budget': 100, 'reasoning_effort': 'low'}, {'enable_thinking': 'on'},
                   {'thinking_budget': -1}, {'thinking_budget': True}, {'preserve_thinking': 1},
                   {'reasoning_effort': 'unknown'}):
        response = await client.post(url, headers=auth(llm), json=payload(llm, key, **fields))
        assert response.status_code == 400
    response = await client.post(url, headers=auth(llm), json=payload(llm, key, enable_thinking=False))
    assert response.status_code == 200
    assert fake.requests[-1][1]['chat_template_kwargs']['enable_thinking'] is False
    assert 'reasoning_effort' not in fake.requests[-1][1]
    await client.put(f'/api/llm/{key}/defaults', json={'thinking': True, 'thinking_mode': 'budget',
                                                    'reasoning_budget_tokens': 1024})
    await client.post(url, headers=auth(llm), json=payload(llm, key))
    assert fake.requests[-1][1]['reasoning_budget_tokens'] == 1024
    await client.put(f'/api/llm/{key}/defaults', json={'thinking_mode': 'effort'})
    await client.post(url, headers=auth(llm), json=payload(llm, key))
    assert fake.requests[-1][1]['reasoning_effort'] == 'xhigh'
    assert 'reasoning_budget_tokens' not in fake.requests[-1][1]
    for route in ('messages', 'responses'):
        response = await client.post(f'/llm/{key}/v1/{route}', headers=auth(llm),
                                     json=payload(llm, key, enable_thinking=False))
        assert response.status_code == 200
        sent = fake.requests[-1][1]
        assert sent.get('thinking') == {'type': 'disabled'} if route == 'messages' else sent['reasoning']['effort'] == 'none'
        await client.post(f'/llm/{key}/v1/{route}', headers=auth(llm),
                          json=payload(llm, key, enable_thinking=True, thinking_budget=5000))
        sent = fake.requests[-1][1]
        if route == 'messages':
            assert sent['thinking'] == {'type': 'enabled', 'budget_tokens': 5000}
            assert sent['output_config']['effort'] == 'medium'
        else:
            assert sent['reasoning']['effort'] == 'medium'
        assert sent['reasoning_budget_tokens'] == 5000
        assert 'reasoning_effort' not in sent


async def test_preserve_thinking_false_removes_only_assistant_reasoning_and_keeps_tools(controls_rig):
    llm, fake, client, _ = controls_rig
    key = create(llm)
    await start(llm, key)
    tool_call = {'id': 'old_call', 'type': 'function', 'function': {'name': 'lookup', 'arguments': '{}'}}
    messages = [{'role': 'user', 'content': 'Keep literal <think>quoted</think> words'},
                {'role': 'assistant', 'reasoning_content': 'secret', 'reasoning': 'also secret',
                 'content': '<think>old thought</think>Final answer', 'tool_calls': [tool_call]},
                {'role': 'tool', 'tool_call_id': 'old_call', 'content': 'tool result'},
                {'role': 'assistant', 'content': [{'type': 'thinking', 'thinking': 'secret'},
                  {'type': 'text', 'text': 'Answer'}, {'type': 'tool_use', 'id': 'new', 'name': 'lookup', 'input': {}}]}]
    body = payload(llm, key, messages=messages, preserve_thinking=False)
    response = await client.post(f'/llm/{key}/v1/chat/completions', headers=auth(llm), json=body)
    assert response.status_code == 200
    sent = fake.requests[-1][1]['messages']
    assert sent[0] == messages[0] and sent[2] == messages[2]
    assert sent[1] == {'role': 'assistant', 'content': 'Final answer', 'tool_calls': [tool_call]}
    assert sent[3]['content'] == messages[3]['content'][1:]
    body['preserve_thinking'] = True
    await client.post(f'/llm/{key}/v1/chat/completions', headers=auth(llm), json=body)
    assert fake.requests[-1][1]['messages'] == messages
    response_input = [{'type': 'reasoning', 'summary': [{'type': 'summary_text', 'text': 'secret'}]},
                      {'type': 'function_call', 'call_id': 'old', 'name': 'lookup', 'arguments': '{}'},
                      {'type': 'function_call_output', 'call_id': 'old', 'output': 'result'}]
    await client.post(f'/llm/{key}/v1/responses', headers=auth(llm),
                      json=payload(llm, key, input=response_input, preserve_thinking=False))
    assert fake.requests[-1][1]['input'] == response_input[1:]


def test_actual_strata_profile_changes_context_threads_and_both_caches():
    profile = {'engine_options': {'max-context': 262144, 'prompt-cache': 6, 'conversation-cache-mib': 4096,
                                 'spec': 4, 'ple-io': 'mmap'}, 'environment': {'OMP_NUM_THREADS': '12'}}
    result = configured_profile(profile, {**default_config(), 'context_tokens': 65535, 'cpu_threads': 24,
                                         'cache_enabled': False, 'cache_mib': 8192})
    assert result['engine_options']['max-context'] == 65535
    assert result['engine_options']['prompt-cache'] == 0
    assert result['engine_options']['conversation-cache-mib'] == 0
    assert result['environment']['OMP_NUM_THREADS'] == '24'
    assert result['engine_options']['spec'] == 4 and result['engine_options']['mmap-experts'] is True
    assert profile['engine_options']['prompt-cache'] == 6 and profile['environment']['OMP_NUM_THREADS'] == '12'
    enabled = configured_profile(profile, {**default_config(), 'cache_mib': 2048})
    assert enabled['engine_options']['prompt-cache'] == 6 and enabled['engine_options']['conversation-cache-mib'] == 2048


def test_loading_progress_keeps_environment_and_verified_download_state_independent(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy_llm_remote, 'ROOT', tmp_path)
    monkeypatch.setattr(deploy_llm_remote, 'STATE', {})
    deploy_llm_remote.status('downloading', '初始化')
    barrier = threading.Barrier(2)

    def environment():
        barrier.wait()
        deploy_llm_remote.stage('environment', 'completed', message='环境已就绪')

    def download():
        barrier.wait()
        deploy_llm_remote.stage('download', 'running', bytes=123, total_bytes=1000, files_done=1,
                               files_total=7, rate_bytes_s=10, elapsed_seconds=12)

    workers = [threading.Thread(target=environment), threading.Thread(target=download)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=3)
        assert not worker.is_alive()
    deploy_llm_remote.status('loading', '加载模型')
    state = json.loads((tmp_path / 'status.json').read_text())
    assert state['phase'] == 'loading' and state['started_at'] <= state['time']
    assert state['stages']['environment']['status'] == 'completed'
    assert state['stages']['download']['bytes'] == 123 and state['stages']['download']['total_bytes'] == 1000
    deploy_llm_remote.stage('download', 'completed', bytes=1000, total_bytes=1000)
    deploy_llm_remote.stage('download', 'running', bytes=123)
    assert json.loads((tmp_path / 'status.json').read_text())['stages']['download']['bytes'] == 1000
