"""Set the sole workbench login using an interactive, non-echoed password prompt."""
import argparse
import getpass
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.access import password_hash  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('username')
    args = parser.parse_args()
    username = args.username.strip()
    if not 1 <= len(username) <= 100:
        parser.error('Username must contain 1–100 characters')
    password = getpass.getpass('Password: ')
    if len(password) < 12 or len(password) > 1024:
        parser.error('Password must contain 12–1024 characters')
    if password != getpass.getpass('Confirm password: '):
        parser.error('Passwords do not match')
    directory = Path(os.environ['WORKBENCH_DATA'])
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = directory / ('access-' + os.urandom(8).hex() + '.tmp')
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump({'username': username, 'password_hash': password_hash(password)}, stream)
    temporary.replace(directory / 'access.json')
    print('Login configuration saved. Restart the workbench to apply and revoke old sessions.')


if __name__ == '__main__':
    main()
