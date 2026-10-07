"""Build only from the retained local owner tag after verifying its image ID."""
import argparse
import json
from pathlib import Path
import subprocess

from verify_local_images import verify


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('context', type=Path)
    parser.add_argument('--tag', required=True)
    args = parser.parse_args()
    manifest = json.loads(Path(__file__).with_name('local-images-20261007.json').read_text())
    base = {'freqtrade': manifest['freqtrade']}
    verify(base)
    subprocess.run([
        'docker', 'build', '--network=none', '--pull=false',
        '--build-arg', 'BASE_IMAGE=' + base['freqtrade']['reference'],
        '-t', args.tag, str(args.context.resolve()),
    ], check=True)
    verify(base)


if __name__ == '__main__':
    main()
