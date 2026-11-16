import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import requests

from backend.colab import Colab
from backend.colab_keepalive import ping_assignment, run_launcher
from tests.fake_colab import PROFILES


@pytest.mark.parametrize('outcome', [200, 204, 401, 403, 404, 500, 'read_timeout', 'connect_timeout'])
def test_assignment_ping_uses_authenticated_colab_endpoint_and_distinguishes_errors(outcome):
    calls = []

    class Response:
        status_code = outcome
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass

    def get(url, **kwargs):
        calls.append((url, kwargs))
        if outcome == 'read_timeout':
            raise requests.exceptions.ReadTimeout('sensitive upstream detail')
        if outcome == 'connect_timeout':
            raise requests.exceptions.ConnectTimeout('sensitive upstream detail')
        return Response()

    client = SimpleNamespace(session=SimpleNamespace(get=get))
    if outcome in (200, 204, 'read_timeout'):
        result = ping_assignment(client, 'synthetic-g4-assignment')
        assert result == {'status': 'sent_without_reply' if outcome == 'read_timeout' else 'acknowledged',
                          'http_status': None if outcome == 'read_timeout' else outcome}
    else:
        with pytest.raises(RuntimeError) as error:
            ping_assignment(client, 'synthetic-g4-assignment')
        assert 'sensitive upstream detail' not in str(error.value)
    assert calls[0][0] == 'https://colab.research.google.com/tun/m/synthetic-g4-assignment/keep-alive/'
    assert calls[0][1]['headers']['X-Colab-Tunnel'] == 'Google'
    assert calls[0][1]['timeout'] == (5, 10)
    assert calls[0][1]['allow_redirects'] is False
    assert 'Authorization' not in calls[0][1]['headers']  # CLI AuthorizedSession adds its own account token.
    calls.clear()
    for invalid in ('../outside', 'https://other.test', '', None):
        with pytest.raises(ValueError):
            ping_assignment(client, invalid)
    assert not calls


async def test_keepalive_adapter_uses_selected_account_python_and_session(tmp_path, monkeypatch):
    profiles = []
    for account in ('colab1', 'colab2'):
        launcher = tmp_path / account
        launcher.write_text('#!' + sys.executable + '\n')
        profiles.append({**PROFILES[0], 'id': account, 'launcher': str(launcher)})
    driver = Colab(profiles, tmp_path)
    invoke = AsyncMock(return_value='WB_KEEPALIVE:' + json.dumps({'status': 'acknowledged', 'http_status': 200}))
    monkeypatch.setattr(driver, 'invoke', invoke)
    await driver.keepalive('colab2', 'synthetic-owned-session')
    command, timeout = invoke.call_args.args
    assert command[0] == sys.executable
    assert command[-2:] == [str(tmp_path / 'colab2'), 'synthetic-owned-session']
    assert command[1].endswith('/backend/colab_keepalive.py') and timeout == 40


def test_helper_runs_through_account_launcher_without_exporting_auth(tmp_path, monkeypatch, capsys):
    commands, calls = {}, []
    def command(**kwargs):
        def register(fn):
            commands[kwargs['name']] = fn
            return fn
        return register
    state = SimpleNamespace(store=SimpleNamespace(get=lambda name: SimpleNamespace(endpoint='synthetic-assignment') if name == 'owned-session' else None),
                            client=object())
    cli = SimpleNamespace(app=SimpleNamespace(command=command), setup_logging=None,
                          auto_update=SimpleNamespace(run_background_check=None))
    monkeypatch.setitem(sys.modules, 'colab_cli', SimpleNamespace(cli=cli))
    monkeypatch.setitem(sys.modules, 'colab_cli.common', SimpleNamespace(state=state))
    monkeypatch.setattr('backend.colab_keepalive.logging.disable', lambda *_: None)
    monkeypatch.setattr('backend.colab_keepalive.ping_assignment', lambda client, endpoint: calls.append((client, endpoint)) or {'status': 'acknowledged', 'http_status': 200})
    launcher = tmp_path / 'colab2'
    launcher.write_text("import sys\nfrom colab_cli import cli\nassert sys.argv[0].endswith('colab2')\ncli.app.invoke(sys.argv[1],sys.argv[2])\n")
    cli.app.invoke = lambda name, session: commands[name](session)
    monkeypatch.setattr(sys, 'argv', [])
    run_launcher(str(launcher), 'owned-session')
    assert calls == [(state.client, 'synthetic-assignment')]
    assert json.loads(capsys.readouterr().out.removeprefix('WB_KEEPALIVE:'))['status'] == 'acknowledged'
    calls.clear()
    with pytest.raises(SystemExit, match='selected account'):
        run_launcher(str(launcher), 'other-session')
    assert not calls
