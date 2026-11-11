import pytest

from scripts.check_publish import issues


@pytest.mark.parametrize('path', [
    '.data/workbench.sqlite3', 'work-backup/copy.json', 'screenshots/account.png',
    'credentials/account.json', '.env.local', 'session-export.json', 'id_ed25519',
    'templates/reference.mp4', 'snapshot.sqlite3-wal',
])
def test_blocks_private_artifacts_even_when_forced_into_git(path):
    assert issues(path, b'local data')


@pytest.mark.parametrize('payload', [
    b'ya29.' + b'a' * 30,
    b'1/' + b'/' + b'a' * 30,
    b'ghp_' + b'a' * 30,
    b'-----BEGIN ' + b'OPENSSH PRIVATE KEY-----',
    b'"refresh_token": "' + b'a' * 30 + b'"',
    b'test-account@' + b'gmail.com',
    b'wb-' + b'abcdef12-2-abcdef',
    b'/Users/' + b'local-user/.ssh/key',
])
def test_reports_secret_type_without_echoing_value(payload):
    found = issues('notes.md', payload)
    assert found
    assert payload.decode() not in '\n'.join(found)


def test_allows_source_and_synthetic_fixture_but_rejects_symlinks():
    assert not issues('backend/config.py', b'credential_present = False')
    assert not issues('tests/fixtures/synthetic.mp4', b'synthetic video')
    assert issues('backend/private-link', b'/outside/credential', mode='120000')
