"""One-time, private import of an idle source workbench into the desktop data directory."""
import argparse
import json
import os
import shutil
import sqlite3
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    source, destination = args.source.resolve(), args.destination.resolve()
    if destination.exists() and any(destination.iterdir()):
        parser.error('Desktop data already exists; refusing to overwrite it')
    database = source / 'workbench.sqlite3'
    with sqlite3.connect(f'file:{database}?mode=ro', uri=True) as original:
        records = [(kind, key, json.loads(body)) for kind, key, body in original.execute('SELECT kind,id,body FROM records')]
        if any(value.get('session') for kind, _, value in records if kind in {'machines', 'llm_instances', 'llm'}):
            parser.error('Source has a live cloud session; finish and release it before importing')
        if any(value.get('status') in {'pending', 'preparing', 'submitting', 'queued', 'running', 'downloading', 'recovering'}
               for kind, _, value in records if kind == 'jobs'):
            parser.error('Source has unfinished jobs; refusing to copy ownership')
        os.umask(0o077)
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        with sqlite3.connect(destination / 'workbench.sqlite3') as copied:
            original.backup(copied)
            copied.execute("DELETE FROM records WHERE kind IN ('access_sessions','accounts','logs','llm_logs')")
            for kind, key, value in records:
                if kind not in {'machines', 'llm_instances', 'llm'}:
                    continue
                value.update(session=None, port=None, telemetry_port=None, phase='stopped', deployment=None,
                             loaded_models=[], message='按需启动 GPU')
                for name in ('legacy_session', 'legacy_model', 'stats', 'loading_started_at', 'created_at', 'last_activity'):
                    if kind == 'machines' or name in {'legacy_session', 'legacy_model', 'stats', 'loading_started_at'}:
                        value.pop(name, None)
                copied.execute('UPDATE records SET body=? WHERE kind=? AND id=?', (json.dumps(value), kind, key))
            copied.commit()
    for name in ('cloud', 'outputs'):
        if (source / name).is_dir():
            shutil.copytree(source / name, destination / name)
    for path in destination.rglob('*'):
        path.chmod(0o700 if path.is_dir() else 0o600)
    print('Imported idle local settings, model profiles and artwork. Account credentials were not copied.')


if __name__ == '__main__':
    main()
