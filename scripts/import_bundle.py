"""Import a verified private Drive index without publishing storage identifiers or credentials."""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.bundles import validate_index  # noqa: E402
from backend.config import DATA  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description='Import a private model-weight bundle index')
    parser.add_argument('manifest', type=Path)
    parser.add_argument('--data', type=Path, default=DATA)
    args = parser.parse_args()
    if args.manifest.stat().st_size > 2 * 1024**2:
        parser.error('Bundle index is too large')
    data = json.loads(args.manifest.read_text())
    key = data.get('preset')
    validate_index(data, key)
    folder = args.data / 'cloud'
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = folder / (key + '.import')
    temporary.write_text(json.dumps(data, indent=2))
    os.chmod(temporary, 0o600)
    temporary.replace(folder / (key + '.json'))
    print('Imported verified bundle:', key)


if __name__ == '__main__':
    main()
