"""Send one Colab assignment activity ping under the selected account launcher.

The account's AuthorizedSession keeps authentication and token refresh local.
No allocation, code execution, inference, credential export or CLI update occurs.
"""
import json
import logging
import re
import sys
from pathlib import Path

import requests


def ping_assignment(client, endpoint):
    if not isinstance(endpoint, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+', endpoint):
        raise ValueError('Invalid assignment endpoint')
    # Colab's Tunnel Frontend records assignment activity on this path. The VM
    # can leave it unanswered; keep the read timeout distinct from HTTP errors.
    url = f'https://colab.research.google.com/tun/m/{endpoint}/keep-alive/'
    try:
        with client.session.get(url, headers={'X-Colab-Tunnel': 'Google', 'X-Colab-Client-Agent': 'colab-cli', 'Accept': 'application/json'},
                                params={'authuser': '0'},
                                timeout=(5, 10), allow_redirects=False, stream=True) as response:
            if not 200 <= response.status_code < 300:
                raise RuntimeError(f'Colab activity ping HTTP {response.status_code}')
            return {'status': 'acknowledged', 'http_status': response.status_code}
    except requests.exceptions.ReadTimeout:
        # Matches the historical CLI behavior; report delivery without a reply,
        # rather than claiming the provider acknowledged or extended its lifetime.
        return {'status': 'sent_without_reply', 'http_status': None}
    except requests.RequestException as exc:
        raise RuntimeError(f'Colab activity ping {type(exc).__name__}') from None


def run_launcher(launcher, session):
    from colab_cli import cli
    from colab_cli.common import state

    # The CLI's generic request logger can include auth headers. Use the
    # authenticated session directly and suppress logging for this child only.
    logging.disable(logging.CRITICAL)
    cli.setup_logging = lambda *_args, **_kwargs: None
    cli.auto_update.run_background_check = lambda: None

    @cli.app.command(name='workbench-keepalive', hidden=True)
    def keepalive(session_name: str):
        assignment = state.store.get(session_name)
        if assignment is None:
            raise SystemExit('Colab activity ping: session is not in the selected account')
        try:
            result = ping_assignment(state.client, assignment.endpoint)
        except Exception as exc:
            # Do not emit credential provider errors, response bodies or URLs.
            detail = str(exc) if isinstance(exc, RuntimeError) and str(exc).startswith('Colab activity ping') else type(exc).__name__
            raise SystemExit(detail) from None
        print('WB_KEEPALIVE:' + json.dumps(result), flush=True)

    # Preserve argv[0] just like bridge.py: managed launchers select the account
    # by their executable name and inject that account's own ADC/profile paths.
    sys.argv = [launcher, 'workbench-keepalive', session]
    source = Path(launcher).resolve()
    exec(compile(source.read_bytes(), str(source), 'exec'), {'__name__': '__main__', '__file__': str(source)})


if __name__ == '__main__':
    run_launcher(*sys.argv[1:])
