"""Send one bounded JSON request to the face agent's private task socket."""
import json
import os
import socket
import sys

path = f'/tmp/face-agent-task-{os.getuid()}.sock'
try:
    request = sys.stdin.buffer.read(25000)
    if len(request) >= 25000:
        raise ValueError('Task request is too large')
    json.loads(request)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(55)
        client.connect(path)
        client.sendall(request.rstrip() + b'\n')
        with client.makefile('rb') as stream:
            response = stream.readline(16384)
    value = json.loads(response)
    print(json.dumps(value))
    sys.exit(0 if value.get('success') else 1)
except Exception as error:
    print(json.dumps({'success': False, 'message': str(error)}))
    sys.exit(1)
