from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from backend.app import create_app
from backend.config import profiles
from backend.manager import Manager
from backend.store import Store
from tests.fake_colab import PROFILES, FakeColab


@pytest.fixture
def account_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('PATH', str(tmp_path / 'bin'))
    return tmp_path


def launcher(root, name):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('#!/bin/sh\nexit 0\n')
    path.chmod(0o700)
    return path


@pytest.mark.parametrize('count', [0, 1, 2, 12])
def test_discovers_actual_accounts_in_numeric_order(account_home, count):
    for number in range(1, count + 1):
        launcher(account_home / '.local/bin', f'colab{number}')
    found = profiles()
    assert [item['id'] for item in found] == [f'colab{number}' for number in range(1, count + 1)]
    assert all(item['installed'] and not item['credential_present'] for item in found)


def test_merges_launchers_and_credential_slots_without_reading_secrets(account_home, monkeypatch):
    launcher(account_home / '.local/bin', 'colab10')
    preferred = launcher(account_home / 'bin', 'colab10')
    launcher(account_home / 'bin', 'colab7')
    for name in ['colab0', 'colab01', 'colab3-backup', 'colab']:
        launcher(account_home / 'bin', name)
    credentials = account_home / '.local/share/colab-accounts/credentials'
    credentials.mkdir(parents=True)
    (credentials / '4.json').write_text('not parsed')
    (credentials / '10.json').write_text('not parsed')
    (credentials / '11.json.backup').touch()
    (credentials / '12.json').mkdir()

    def no_content_reads(*args, **kwargs):
        raise AssertionError('Discovery must not read credential contents')

    monkeypatch.setattr(Path, 'read_text', no_content_reads)
    found = profiles()
    assert [item['id'] for item in found] == ['colab4', 'colab7', 'colab10']
    assert found[0]['credential_present'] and not found[0]['installed']
    assert found[2]['credential_present'] and found[2]['installed']
    assert found[2]['launcher'] == str(preferred)


async def test_explicit_empty_accounts_does_not_discover_real_accounts(account_home, tmp_path):
    launcher(account_home / '.local/bin', 'colab7')
    app = create_app(tmp_path / 'data', driver=FakeColab(), account_profiles=[])
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as client:
            assert (await client.get('/api/state')).json()['accounts'] == []


async def test_removed_launcher_keeps_live_session_visible_on_restart(account_home, tmp_path):
    directory = tmp_path / 'data'
    store = Store(directory)
    store.put('settings', 'main', {'identity': '', 'idle_minutes': 0, 'stop_on_exit': False})
    store.put('machines', 'colab10', {'account': 'colab10', 'phase': 'ready', 'session': 'test-retained-session', 'port': 12345})
    store.close()
    app = create_app(directory, driver=FakeColab())
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as client:
            accounts = (await client.get('/api/state')).json()['accounts']
            assert len(accounts) == 1
            assert accounts[0]['id'] == 'colab10'
            assert not accounts[0]['installed']
            assert accounts[0]['machine']['phase'] == 'disconnected'
            assert accounts[0]['machine']['session'] == 'test-retained-session'


async def test_multi_digit_account_session_ownership_is_distinct(tmp_path):
    store, fake = Store(tmp_path), FakeColab()
    expanded = [PROFILES[0], {**PROFILES[1], 'id': 'colab11'}]
    fake.active['colab11'] = []
    manager = Manager(store, fake, expanded)
    await manager.open()
    try:
        await manager.start('colab11')
        machine = manager.machine('colab11')
        assert machine['phase'] == 'ready'
        assert machine['session'].startswith(f"wb-{store.get('meta', 'install')['id']}-11-")
        manager.machine('colab1', session=machine['session'], phase='ready')
        await manager.stop('colab1')
        assert manager.machine('colab1')['phase'] == 'error'
        assert not any(call[:2] == ('colab1', 'stop') for call in fake.calls)
        manager.machine('colab1', session=None, phase='stopped')
        await manager.stop('colab11')
        assert manager.machine('colab11')['session'] is None
        assert fake.active['colab1'][0]['name'] == 'unrelated-important-session'
    finally:
        await manager.close()
        store.close()
