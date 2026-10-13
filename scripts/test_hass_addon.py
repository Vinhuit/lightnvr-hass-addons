"""Smoke-test the add-on in disposable Docker containers, without Supervisor.

Uses an isolated network to simulate Supervisor's Ingress source address.
Never mounts an existing NVR's configuration or camera recordings.
"""
import argparse
import base64
import http.cookiejar
import ipaddress
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid


def docker(*args):
    return subprocess.check_output(['docker', *args], text=True).strip()


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def port(container, internal):
    bindings = json.loads(docker('inspect', container))[0]['NetworkSettings']['Ports']
    return int(bindings[f'{internal}/tcp'][0]['HostPort'])


def wait_ready(url):
    for _ in range(90):
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(1)
    raise RuntimeError('Add-on did not become ready within 90 seconds')


def login(opener, base, password):
    request = urllib.request.Request(base + 'api/auth/login',
        data=json.dumps({'username': 'admin', 'password': password}).encode(),
        headers={'Content-Type': 'application/json'})
    with opener.open(request, timeout=15) as response:
        check(response.status == 200, 'Login failed')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--image', default='lightnvr-hass-addon:0.1.0')
    parser.add_argument('--keep', action='store_true', help='Keep fixtures after success for manual browser testing')
    parser.add_argument('--bind-address', default='127.0.0.1')
    parser.add_argument('--test-subnet', default='172.30.32.0/29', help='Alternate isolated subnet if the production Supervisor address is already routed')
    args = parser.parse_args()
    subnet = ipaddress.ip_network(args.test_subnet)
    supervisor_ip = str(subnet.network_address + 2)
    app_ip = str(subnet.network_address + 3)
    existing = docker('network', 'ls', '-q').splitlines()
    for network in json.loads(docker('network', 'inspect', *existing)):
        for entry in network['IPAM'].get('Config') or []:
            if entry.get('Subnet') and ipaddress.ip_network(entry['Subnet']).overlaps(subnet):
                raise RuntimeError('The Supervisor test subnet is already in use; run on a separate Docker host')

    suffix = uuid.uuid4().hex[:8]
    network = 'lightnvr-addon-test-' + suffix
    app = network + '-app'
    supervisor = network + '-supervisor'
    folder = Path(tempfile.mkdtemp(prefix=network + '-'))
    for name in ('config', 'data', 'media'):
        (folder / name).mkdir()
    password = 'AddonTestPassword123'
    (folder / 'data/options.json').write_text(json.dumps({'initial_admin_password': password, 'retention_days': 7, 'log_level': 'warning'}))
    prefix = '/api/hassio_ingress/addon-test/'
    production_nginx = docker('run', '--rm', '--entrypoint', 'cat', args.image, '/etc/nginx/lightnvr-addon.conf')
    check('allow 172.30.32.2;' in production_nginx and 'deny all;' in production_nginx, 'Production Ingress source restriction is missing')
    (folder / 'ingress.conf').write_text(production_nginx.replace('allow 172.30.32.2;', f'allow {supervisor_ip};'))
    (folder / 'supervisor.conf').write_text('''
events {}
http {
  server {
    listen 8099;
    location /api/hassio_ingress/addon-test/ {
      proxy_pass http://172.30.32.3:8099/;
      proxy_http_version 1.1;
      proxy_set_header X-Ingress-Path /api/hassio_ingress/addon-test;
      proxy_set_header Host $http_host;
      proxy_set_header X-Forwarded-Proto $scheme;
      proxy_set_header Upgrade $http_upgrade;
      proxy_set_header Connection upgrade;
      proxy_buffering off;
    }
  }
}
'''.replace('172.30.32.3', app_ip))

    def start_app():
        return docker('run', '-d', '--init', '--name', app, '--network', network, '--ip', app_ip,
            '-p', args.bind_address + '::8080',
            '-v', str(folder / 'config') + ':/etc/lightnvr',
            '-v', str(folder / 'data') + ':/data',
            '-v', str(folder / 'media') + ':/media',
            '-v', str(folder / 'ingress.conf') + ':/etc/nginx/lightnvr-addon.conf:ro', args.image)

    success = False
    try:
        docker('network', 'create', '--subnet', str(subnet), network)
        start_app()
        docker('run', '-d', '--name', supervisor, '--network', network, '--ip', supervisor_ip,
            '-p', args.bind_address + '::8099', '-v', str(folder / 'supervisor.conf') + ':/tmp/supervisor.conf:ro',
            '--entrypoint', 'nginx', args.image, '-c', '/tmp/supervisor.conf', '-g', 'daemon off;')
        direct_port = port(app, 8080)
        ingress_port = port(supervisor, 8099)
        direct = f'http://127.0.0.1:{direct_port}/'
        ingress = f'http://127.0.0.1:{ingress_port}' + prefix
        wait_ready(direct + 'login.html')
        wait_ready(ingress + 'login.html')

        # Other callers cannot access the Ingress listener even with forged headers.
        forbidden = docker('exec', app, 'curl', '-s', '-o', '/dev/null', '-w', '%{http_code}',
            '-H', 'X-Forwarded-For: 172.30.32.2', 'http://127.0.0.1:8099/')
        check(forbidden == '403', 'Ingress listener accepted a non-Supervisor caller')
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        login(opener, ingress, password)
        check(any(cookie.path.startswith(prefix) for cookie in jar), 'Ingress session cookie escaped the app path')
        with opener.open(ingress + 'api/streams', timeout=15) as response:
            check(response.status == 200, 'Authenticated Ingress API failed')
        with opener.open(ingress + 'index.html', timeout=15) as response:
            check(b'LightNVR' in response.read(), 'Ingress HTML is missing')

        # Test the WebSocket upgrade through both proxy layers.
        request = urllib.request.Request(ingress + 'go2rtc/api/ws?src=addon-test-missing')
        jar.add_cookie_header(request)
        headers = (
            f'GET {prefix}go2rtc/api/ws?src=addon-test-missing HTTP/1.1\r\n'
            f'Host: 127.0.0.1:{ingress_port}\r\n'
            'Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\n'
            f'Sec-WebSocket-Key: {base64.b64encode(b"addon-test-key16").decode()}\r\n'
            f'Cookie: {request.get_header("Cookie", "")}\r\n\r\n')
        with socket.create_connection(('127.0.0.1', ingress_port), timeout=10) as websocket:
            websocket.sendall(headers.encode())
            first = websocket.recv(4096).split(b'\r\n', 1)[0]
            check(b'101' in first, 'WebSocket upgrade failed: ' + first.decode())

        marker = '/media/lightnvr/recordings/persistence-test.txt'
        docker('exec', app, 'python3', '-c', f'from pathlib import Path; Path({marker!r}).write_text("persistent media fixture")')
        check((folder / 'data/database/lightnvr.db').exists(), 'Database is not in the persistent volume')
        # Container recreation must retain credentials and database, independent of the image filesystem.
        docker('stop', '--time', '30', app)
        check(json.loads(docker('inspect', app))[0]['State']['ExitCode'] == 0, 'Container did not shut down gracefully')
        docker('rm', '-v', app)
        (folder / 'data/options.json').write_text(json.dumps({'initial_admin_password': 'ChangedOptionOnly123', 'retention_days': 3, 'log_level': 'warning'}))
        start_app()
        wait_ready(f'http://127.0.0.1:{port(app, 8080)}/login.html')
        jar.clear()
        login(opener, ingress, password)
        check(docker('exec', app, 'cat', marker) == 'persistent media fixture', 'Media was lost on recreation')
        config = docker('exec', app, 'cat', '/etc/lightnvr/lightnvr.ini')
        check('retention_days = 3' in config, 'Updated retention was not applied')
        success = True
        print(json.dumps({'result': 'PASS', 'checks': ['startup', 'login', 'Supervisor-only Ingress', 'cookie path', 'authenticated API', 'WebSocket upgrade', 'graceful stop', 'recreation persistence'],
            'ingress_port': ingress_port, 'fixtures': str(folder), 'containers': [app, supervisor], 'network': network, 'kept': args.keep}))
    except Exception:
        # Configuration fixtures contain test credentials only, never real NVR data.
        for name in (app, supervisor):
            subprocess.run(['docker', 'logs', '--tail', '35', name], check=False)
        raise
    finally:
        if not success or not args.keep:
            for name in (app, supervisor):
                subprocess.run(['docker', 'rm', '-f', '-v', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(['docker', 'network', 'rm', network], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            # The unique fixture directory is kept for inspection; no recursive deletion.


if __name__ == '__main__':
    main()
