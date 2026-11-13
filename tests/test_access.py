import hashlib
import json
import time

import pytest
from httpx import ASGITransport, AsyncClient

from backend.access import COOKIE, Access, password_hash
from backend.app import create_app
from backend.store import Store
from tests.fake_colab import PROFILES, FakeColab


@pytest.fixture
async def secured(tmp_path):
    config = {'username': 'tester', 'password_hash': password_hash('test-password')}
    (tmp_path / 'access.json').write_text(json.dumps(config))
    app = create_app(tmp_path, driver=FakeColab(), account_profiles=PROFILES)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as client:
            csrf = (await client.get('/api/auth/session')).json()['csrf']
            client.headers['x-workbench-token'] = csrf
            yield client, app


async def login(client):
    return await client.post('/api/auth/login', json={'username': 'tester', 'password': 'test-password'})


async def test_login_protects_data_and_rotates_then_revokes_cookie(secured):
    client, app = secured
    for path in ['/api/state', '/api/template', '/api/jobs/example/files/file.png', '/reference/reference.jpg']:
        assert (await client.get(path)).status_code == 401
    assert (await client.post('/api/accounts/colab2/start')).status_code == 401
    assert (await client.post('/api/auth/login', json={'username': 'tester', 'password': 'wrong'})).status_code == 401
    response = await login(client)
    assert response.status_code == 200
    assert 'httponly' in response.headers['set-cookie'].lower()
    first = client.cookies[COOKIE]
    assert (await client.get('/api/state')).status_code == 200
    assert (await client.get('/api/auth/session')).json()['username'] == 'tester'
    assert (await client.post('/api/settings', headers={'x-workbench-token': 'wrong'})).status_code == 403
    assert (await login(client)).status_code == 200
    assert first != client.cookies[COOKIE]
    assert app.state.store.get('access_sessions', hashlib.sha256(first.encode()).hexdigest()) is None
    assert (await client.post('/api/auth/logout')).status_code == 200
    assert (await client.get('/api/state')).status_code == 401
    assert not app.state.store.all('access_sessions')


async def test_login_csrf_rate_limit_expiry_and_no_registration(secured):
    client, app = secured
    assert (await client.post('/api/auth/login', json={'username': 'tester', 'password': 'test-password'},
                              headers={'origin': 'https://evil.test'})).status_code == 403
    assert (await client.post('/api/auth/login', json={'username': 'tester', 'password': 'test-password'},
                              headers={'x-workbench-token': 'wrong'})).status_code == 403
    for _ in range(5):
        assert (await client.post('/api/auth/login', json={'username': 'unknown', 'password': 'wrong'})).status_code == 401
    assert (await login(client)).status_code == 429
    app.state.access.failures.clear()
    await login(client)
    key = hashlib.sha256(client.cookies[COOKIE].encode()).hexdigest()
    app.state.store.patch('access_sessions', key, expires=time.time() - 1)
    assert (await client.get('/api/state')).status_code == 401
    await login(client)
    assert (await client.post('/api/auth/register', json={})).status_code in {404, 405}
    # Agent endpoints keep their existing bearer authentication rather than using browser cookies.
    assert (await client.get('/llm/unknown/v1/models')).status_code == 401


def test_missing_required_configuration_fails_closed_and_hash_is_salted(tmp_path):
    store = Store(tmp_path)
    try:
        with pytest.raises(RuntimeError, match='登录配置'):
            Access(tmp_path, store, required=True)
        assert password_hash('test-password') != password_hash('test-password')
    finally:
        store.close()


async def test_sessions_survive_process_config_reload_but_not_password_change(secured):
    client, app = secured
    await login(client)
    original = app.state.access
    app.state.access = Access(original.path.parent, app.state.store, required=True)
    assert (await client.get('/api/state')).status_code == 200
    config = {'username': 'tester', 'password_hash': password_hash('new-test-password')}
    original.path.write_text(json.dumps(config))
    app.state.access = Access(original.path.parent, app.state.store, required=True)
    assert (await client.get('/api/state')).status_code == 401
