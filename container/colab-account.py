#!/opt/colab-cli/bin/python
"""Pinned ADC launcher: each numbered profile has separate credentials and state."""
import json
import os
import re
import sys
from pathlib import Path


def run():
    os.umask(0o077)
    name = Path(sys.argv[0]).name
    match = re.fullmatch(r'colab([1-9][0-9]*)', name)
    if not match:
        raise SystemExit('Use a numbered colab account launcher.')
    account = match[1]
    credential = Path.home() / '.local/share/colab-accounts/credentials' / f'{account}.json'
    args = sys.argv[1:]
    if args and args[0] in {'login', 'logout', 'update'}:
        raise SystemExit('Manage credentials and the pinned CLI runtime outside this container.')
    if not credential.is_file():
        raise SystemExit('Account credentials are not configured.')
    os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = str(credential)
    from colab_cli import auth, auto_update
    from colab_cli.common import state
    from colab_cli.history import HistoryLogger
    from colab_cli.state import SettingsStore

    scopes = json.loads(credential.read_text()).get('scopes')
    if isinstance(scopes, list) and scopes and all(isinstance(item, str) for item in scopes):
        auth.PUBLIC_SCOPES = scopes
    for index, arg in enumerate(args):
        if arg == '--':
            break
        if arg.startswith('--auth=') and arg != '--auth=adc':
            raise SystemExit('Account launchers require ADC authentication.')
        if arg == '--auth' and index + 1 < len(args) and args[index + 1] != 'adc':
            raise SystemExit('Account launchers require ADC authentication.')
        if arg == '--config' or arg.startswith('--config='):
            raise SystemExit('Account configuration is isolated by the launcher.')
    profile = Path.home() / '.config/colab-cli/accounts' / account
    profile.mkdir(parents=True, exist_ok=True)
    state._history = HistoryLogger(str(profile / 'history'))
    state._settings_store = SettingsStore(str(profile / 'settings.json'))


    def managed_update():
        raise SystemExit('Update the pinned container CLI runtime as one unit.')


    auto_update.self_install = managed_update
    sys.argv = [name, '--auth', 'adc', '--config', str(profile / 'sessions.json'), *args]
    from colab_cli.cli import main

    main()


if __name__ == "__main__":
    run()
