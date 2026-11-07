"""Exercise the deployed broker without production data or credentials."""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import http.client
import io
import json
import socket
import time
import zipfile
from contextlib import closing
from pathlib import Path


class Connection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__('localhost', timeout=75)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


def execute(path, code):
    content = b'item,quantity\nbeaker,2\n'
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        archive.writestr('program.py', code)
        archive.writestr('manifest.json', json.dumps([{'path': '/inputs/0.csv', 'filename': 'verification.csv', 'sha256': hashlib.sha256(content).hexdigest()}]))
        archive.writestr('inputs/0.csv', content)
    with closing(Connection(path)) as client:
        client.request('POST', '/execute', body=stream.getvalue(), headers={'Content-Type': 'application/zip'})
        response = client.getresponse()
        body, status = response.read(), response.status
    if status != 200:
        return {'http_status': status}
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        return {'http_status': status, 'state': json.loads(archive.read('__analysis__.json')),
                'files': {name: archive.read(name).decode('utf-8') for name in archive.namelist() if name != '__analysis__.json'}}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--socket', default='/run/lab-analysis/broker.sock')
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    results = {}
    isolation = execute(args.socket, '''import json,os,socket
from pathlib import Path
paths=['/var/lib/lab-management/consumables.db','/etc/lab-management/environment','/var/backups/lab-management','/var/lib/lab-dsh','/etc/lab-dsh','/inputs/0.csv']
access={}
for path in paths:
 try:
  with open(path,'rb') as stream: access[path]=bool(stream.read(1))
 except OSError: access[path]=False
network={}
for host,port in [('10.15.171.204',30969),('1.1.1.1',443)]:
 try:
  with socket.create_connection((host,port),timeout=1): network[host]=True
 except OSError: network[host]=False
writeable=True
try: Path('/inputs/0.csv').write_text('changed')
except OSError: writeable=False
Path('/output/check.json').write_text(json.dumps(dict(access=access,network=network,input_writeable=writeable,cpu=Path('/sys/fs/cgroup/cpu.max').read_text().strip(),memory=Path('/sys/fs/cgroup/memory.max').read_text().strip(),uid=os.getuid(),environment=dict(os.environ))))
''')
    assert isolation.get('state', {}).get('status') == 'completed', isolation
    check = json.loads(isolation['files']['check.json'])
    assert check['access']['/inputs/0.csv'] is True and sum(check['access'].values()) == 1, check
    assert not any(check['network'].values()) and check['input_writeable'] is False, check
    assert check['uid'] == 65534 and check['memory'] == '1073741824', check
    cpu, period = check['cpu'].split()
    assert cpu != 'max' and int(cpu) <= int(period), check
    assert not any('KEY' in key or 'TOKEN' in key or 'PASSWORD' in key for key in check['environment']), check
    results['isolation'] = check
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(execute, args.socket, 'import time; time.sleep(3)')
        time.sleep(1)
        second = execute(args.socket, 'print(2)')
        assert second['http_status'] == 429, second
        assert first.result()['state']['status'] == 'completed'
        results['single_concurrency'] = True
    memory = execute(args.socket, 'value=bytearray(1200*1024*1024); print(len(value))')
    assert memory.get('state', {}).get('status') == 'failed', memory
    results['memory_termination'] = memory['state']
    output = execute(args.socket, "from pathlib import Path; Path('/output/large.bin').write_bytes(b'x'*(51*1024*1024))")
    assert output['http_status'] == 422 or output.get('state', {}).get('status') == 'failed', output
    results['output_limit'] = {'http_status': output['http_status'], 'status': output.get('state', {}).get('status')}
    started = time.monotonic()
    timeout = execute(args.socket, 'while True: pass')
    elapsed = time.monotonic() - started
    assert timeout.get('state', {}).get('status') == 'timed_out' and elapsed < 65, (timeout, elapsed)
    results['timeout'] = {'status': 'timed_out', 'elapsed_seconds': round(elapsed, 2)}
    results['recovered'] = execute(args.socket, 'print("verified")')['state']['status'] == 'completed'
    assert results['recovered']
    args.report.write_text(json.dumps(results, indent=2))
    print(json.dumps(results))


if __name__ == '__main__':
    main()
