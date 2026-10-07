#!/usr/bin/env python3
"""Type animation states into a terminal to drive the local robot display."""
import argparse
import json
import os
import shlex
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def request(base, path, payload=None):
    body = json.dumps(payload).encode() if payload is not None else None
    try:
        with urlopen(Request(base.rstrip('/') + '/api/' + path, data=body,
                             headers={'Content-Type': 'application/json'}), timeout=120 if path == 'say' else 5) as response:
            return json.load(response)
    except HTTPError as error:
        raise ValueError(json.load(error).get('error', str(error))) from None
    except URLError:
        raise ValueError('Start the local service with ./run.sh and open robot.html.') from None


def execute(base, text):
    words = shlex.split(text)
    if not words:
        return
    if words[0] in ('quit', 'exit'):
        return False
    if words[0] in ('help', 'states', 'list'):
        status = request(base, 'status')
        print('Commands: states, status, quit, say [text]. Animation: sleeping, thinking, speaking [0..1], idle, wave, …')
        for action in status['actions']:
            print(f"  {action['id']:<30} {action['label']}")
        if not status['actions']:
            print('Open the studio or robot display to load its original animation library.')
        return
    if words[0] == 'status':
        print(json.dumps(request(base, 'status'), indent=2)); return
    if words[0].lower() == 'say':
        result = request(base, 'say', dict(stream=True, **(dict(text=' '.join(words[1:])) if len(words) > 1 else {})))
        print('→ speech sent to robot display' if result['displays'] else '→ speech queued; open robot.html')
        return
    level = None
    if words[0].lower() in ('speaking', 'speak') and len(words) > 1:
        if len(words) != 2:
            raise ValueError('Use speaking or speaking 0.5.')
        level = float(words.pop())
    result = request(base, 'command', dict(state=' '.join(words), level=level))
    print(f"→ {result['state']}" + (f" · level {level:g}" if level is not None else '') +
          (f" · {result['displays']} display(s)" if result['displays'] else ' · queued; open robot.html'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default=os.environ.get('DOTS_URL', 'http://127.0.0.1:5173'))
    parser.add_argument('state', nargs='*', help='One-shot state; omit to open the interactive prompt.')
    args = parser.parse_args()
    if args.state:
        try:
            execute(args.url, ' '.join(args.state)); return 0
        except (ValueError, OSError) as error:
            print(str(error), file=sys.stderr); return 1
    print(f'Dots terminal · {args.url}\nType a state, states to list animations, or quit.')
    while True:
        try:
            if execute(args.url, input('dots> ')) is False:
                break
        except (EOFError, KeyboardInterrupt):
            print(); break
        except (ValueError, OSError) as error:
            print(str(error), file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
