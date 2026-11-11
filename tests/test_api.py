import pytest
from httpx import ASGITransport, AsyncClient

from backend.app import create_app
from backend.colab import Colab, clean
from backend.workflows import validate_graph
from tests.fake_colab import PROFILES, FakeColab, template


@pytest.fixture
async def client(tmp_path):
    app = create_app(tmp_path, driver=FakeColab(), account_profiles=PROFILES)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as client:
            token = (await client.get('/api/state')).json()['token']
            client.headers['x-workbench-token'] = token
            yield client


async def test_mutations_require_same_origin_and_csrf(client):
    response = await client.post('/api/accounts/colab2/start', headers={'origin': 'https://evil.test'})
    assert response.status_code == 403
    response = await client.post('/api/accounts/colab2/start', headers={'x-workbench-token': 'wrong'})
    assert response.status_code == 403
    response = await client.get('/api/state', headers={'host': 'evil.test'})
    assert response.status_code == 400


async def test_draft_roundtrip_and_invalid_ui_workflow(client):
    body = {'name': 'Local edit', 'graph': template(), 'positions': {'1': {'x': 50, 'y': 60}}}
    assert (await client.put('/api/drafts/main', json=body)).status_code == 200
    restored = (await client.get('/api/drafts/main')).json()
    assert restored['positions'] == body['positions']
    assert restored['graph'] == body['graph']
    invalid = {**body, 'graph': {'nodes': [], 'links': []}}
    assert (await client.put('/api/drafts/main', json=invalid)).status_code == 400


async def test_unknown_account_and_files_are_not_accessible(client):
    assert (await client.post('/api/accounts/other/start')).status_code == 400
    assert (await client.get('/api/jobs/absent/files/secrets.json')).status_code == 404
    assert (await client.get('/reference/secret')).status_code == 404


def test_rejects_cycles_dangling_references_and_bad_dimensions():
    graph = template()
    graph['1']['inputs']['model'] = ['7', 0]
    with pytest.raises(ValueError, match='循环'):
        validate_graph(graph)
    graph = template()
    graph['12']['inputs']['noise'] = ['missing', 0]
    with pytest.raises(ValueError, match='不存在'):
        validate_graph(graph)
    graph = template()
    graph['5']['inputs']['width'] = 99999
    with pytest.raises(ValueError, match='width'):
        validate_graph(graph)


async def test_cli_parser_preserves_other_sessions_and_fails_closed(tmp_path):
    cli = Colab(PROFILES, tmp_path)
    async def valid(*args):
        return '[external] ep1 | Hardware: G4 | Shape: High-RAM | Variant: GPU\n[wb-test] ep2 | Hardware: G4 | Shape: Standard | Variant: GPU\n'
    cli.command = valid
    sessions = await cli.sessions('colab2')
    assert [s['name'] for s in sessions] == ['external', 'wb-test']
    async def invalid(*args):
        return 'unrecognized output'
    cli.command = invalid
    with pytest.raises(RuntimeError, match='无法解析'):
        await cli.sessions('colab2')


def test_redacts_tokens_before_logging():
    assert 'private-value' not in clean('refresh_token: private-value')
    assert 'abc123' not in clean('colab-runtime-proxy-token=abc123&next=1')
