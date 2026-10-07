#!/usr/bin/env python3
"""Import the original renderer from an existing local desktop installation."""
import argparse
import json
import pathlib
import struct

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('archive', nargs='?', default='/usr/lib/chatgpt/resources/app.asar')
args = parser.parse_args()
archive = pathlib.Path(args.archive)
destination = pathlib.Path(__file__).resolve().parents[1] / 'public' / 'local-dots'
with archive.open('rb') as source:
    header = struct.unpack('<4I', source.read(16))
    tree = json.loads(source.read(header[3]))
    assets = tree['files']['webview']['files']['assets']['files']
    matches = [name for name, node in assets.items()
               if name.startswith('orbit-character-') and 'runtime' in node.get('files', {})]
    if len(matches) != 1:
        raise SystemExit(f'Expected one character runtime, found {len(matches)}. Installation layout may have changed.')
    name = matches[0]
    runtime = assets[name]['files']['runtime']['files']
    required = ['orbit-characters.mjs', 'orbit-characters.wasm', 'orbit-characters.data',
                'orbit-enums.mjs', 'orbit-characters.d.mts', 'orbit-enums.d.mts',
                'bundle.json', 'THIRD_PARTY_NOTICES.txt']
    for filename in required:
        if filename not in runtime:
            raise SystemExit(f'The installation is missing {filename}. No files imported.')
    destination.mkdir(parents=True, exist_ok=True)
    for filename in required:
        item = runtime[filename]
        if item.get('unpacked'):
            content = (pathlib.Path(str(archive) + '.unpacked') / 'webview' / 'assets' / name / 'runtime' / filename).read_bytes()
        else:
            source.seek(8 + header[1] + int(item['offset']))
            content = source.read(item['size'])
            if len(content) != item['size']:
                raise SystemExit(f'Truncated resource: {filename}')
        (destination / filename).write_bytes(content)
metadata = json.loads((destination / 'bundle.json').read_text())
print(f'Imported installed dots renderer {metadata["sdkVersion"]} to {destination}')
print('Assets are included in repository packaging. Rebuild before using run.sh.')
