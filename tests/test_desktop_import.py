import subprocess
import sys

from backend.config import ROOT
from backend.store import Store


def invoke(source, target):
    return subprocess.run([sys.executable, str(ROOT / 'desktop/import-local.py'), str(source), str(target)],
                          capture_output=True, text=True)


def test_desktop_import_preserves_idle_configuration_without_login_or_session_state(tmp_path):
    source, target = tmp_path / 'source', tmp_path / 'desktop'
    store = Store(source)
    store.put('settings', 'main', {'identity': '/test/ssh/key', 'idle_minutes': 5, 'stop_on_exit': True})
    store.put('llm_instances', 'svc-example', {'id': 'svc-example', 'name': 'Personal', 'phase': 'stopped',
                                            'session': None, 'port': 12345, 'deployment': {'phase': 'ready'}})
    store.put('access_sessions', 'example', {'username': 'tester'})
    store.put('accounts', 'colab1', {'sessions': [{'name': 'external'}]})
    store.put('drafts', 'main', {'name': 'Draft'})
    (source / 'cloud').mkdir()
    (source / 'cloud/example.json').write_text('{}')
    (source / 'access.json').write_text('{}')
    store.close()
    assert invoke(source, target).returncode == 0
    copied = Store(target)
    try:
        assert copied.get('settings', 'main')['idle_minutes'] == 5
        assert copied.get('drafts', 'main')['name'] == 'Draft'
        instance = copied.get('llm_instances', 'svc-example')
        assert instance['name'] == 'Personal' and instance['port'] is None and instance['session'] is None
        assert not copied.all('access_sessions') and not copied.all('accounts')
        assert (target / 'cloud/example.json').exists() and not (target / 'access.json').exists()
        assert invoke(source, target).returncode != 0
    finally:
        copied.close()


def test_desktop_import_rejects_live_cloud_ownership_before_writing(tmp_path):
    source, target = tmp_path / 'source', tmp_path / 'desktop'
    store = Store(source)
    store.put('machines', 'colab1', {'session': 'synthetic-active-session', 'phase': 'ready'})
    store.close()
    result = invoke(source, target)
    assert result.returncode != 0 and 'live cloud session' in result.stderr
    assert not target.exists()
