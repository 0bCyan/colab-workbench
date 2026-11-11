"""Inspect the Git index before commit or in CI; never print secret values."""
import re
import subprocess
import sys
from pathlib import PurePosixPath

PRIVATE_DIRS = {
    '.data', 'work-backup', 'backups', 'screenshots', 'outputs', '.ssh', '.local',
    '.config', 'credentials', 'secrets', 'sessions', 'colab-accounts',
    'node_modules', '.venv', 'dist', 'test-results', 'playwright-report', 'blob-report',
}
PRIVATE_SUFFIXES = {
    '.sqlite', '.sqlite3', '.db', '.pem', '.key', '.p12', '.pfx', '.bak', '.backup',
    '.log', '.zip', '.tar', '.gz', '.safetensors', '.ckpt', '.gguf',
    '.mp4', '.mov', '.webm', '.wav', '.mp3',
}
PATTERNS = {
    'private key': re.compile(rb'-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----'),
    'Google OAuth token': re.compile(rb'(?:ya29\.[A-Za-z0-9_-]{20,}|1//[A-Za-z0-9_-]{20,})'),
    'GitHub token': re.compile(rb'(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})'),
    'cloud access key': re.compile(rb'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b'),
    'Hugging Face token': re.compile(rb'\bhf_[A-Za-z0-9]{20,}'),
    'OAuth credential value': re.compile(
        rb'["\'](?:refresh_token|access_token|client_secret|private_key)["\']\s*:\s*["\'][^"\'\r\n]{12,}["\']'
    ),
    'Google account address': re.compile(rb'[A-Za-z0-9._%+-]+@(?:gmail|googlemail)\.com\b', re.I),
    'Google OAuth client ID': re.compile(rb'\d{10,}-[a-z0-9]+\.apps\.googleusercontent\.com'),
    'personal home path': re.compile(rb'/(?:Users|home)/[A-Za-z0-9_.-]+/'),
    'real workbench session ID': re.compile(rb'\bwb-[0-9a-f]{8}-\d+-[0-9a-f]{6}\b'),
}


def issues(path, data, mode='100644'):
    name = PurePosixPath(path)
    if mode not in {'100644', '100755'}:
        return ['symlinks and submodules are not publishable']
    if any(part in PRIVATE_DIRS for part in name.parts):
        return ['local or generated directory']
    if name.name == 'validation-result.json' or name.match('templates/reference.*'):
        return ['local validation artifact']
    if (name.name.startswith('.env') and name.name != '.env.example') or re.match(
        r'id_(?:rsa|ed25519|ecdsa)', name.name
    ):
        return ['local environment or SSH identity file']
    if re.search(r'(?:credential|session|token|secret).*\.json$', name.name, re.I):
        return ['credential or session export']
    if re.search(r'\.(?:sqlite3?|db)(?:-.+)?$', name.name, re.I):
        return ['local database']
    if path != 'tests/fixtures/synthetic.mp4' and name.suffix.lower() in PRIVATE_SUFFIXES:
        return ['private, generated, or model file type']
    if len(data) > 2_000_000:
        return ['file exceeds the source repository size limit']
    found = []
    for label, pattern in PATTERNS.items():
        match = pattern.search(data)
        if match:
            line = data[:match.start()].count(b'\n') + 1
            found.append(f'{label} at line {line}')
    return found


def main():
    index = subprocess.check_output(['git', 'ls-files', '--stage', '-z'])
    failed, count = False, 0
    for entry in index.split(b'\0'):
        if not entry:
            continue
        metadata, raw_path = entry.split(b'\t', 1)
        mode, object_id, stage = metadata.decode().split()
        path = raw_path.decode()
        if stage != '0':
            print(f'{path}: unresolved merge conflict', file=sys.stderr)
            failed = True
            continue
        data = subprocess.check_output(['git', 'cat-file', 'blob', object_id]) if mode in {'100644', '100755'} else b''
        count += 1
        for problem in issues(path, data, mode):
            print(f'{path}: {problem}', file=sys.stderr)
            failed = True
    if failed:
        print('Commit blocked. Keep private data outside the tracked source tree.', file=sys.stderr)
        return 1
    print(f'Publish check passed: {count} indexed files.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
