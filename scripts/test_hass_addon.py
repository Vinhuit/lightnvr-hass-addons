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
    parser.add_argument('--image', default='lightnvr-hass-addon:0.3.0')
    parser.add_argument('--keep', action='store_true', help='Keep fixtures after success for manual browser testing')
    parser.add_argument('--generated-password', action='store_true', help='Exercise the default first-run generated password')
    parser.add_argument('--failure-service', choices=['detector', 'ytstream'], default='detector')
    parser.add_argument('--browser-fixture', action='store_true', help='Keep a running isolated fixture for browser verification')
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
    password = 'A' * 31
    (folder / 'data/options.json').write_text(json.dumps({'initial_admin_password': '' if args.generated_password else password, 'retention_days': 7, 'log_level': 'warning'}))
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
        yt_base = ingress + 'ytstream/'
        wait_ready(yt_base + 'health')
        if args.browser_fixture:
            success = True
            args.keep = True
            print(json.dumps({'ingress_port': ingress_port, 'containers': [app, supervisor], 'network': network, 'fixtures': str(folder)}))
            return
        with urllib.request.urlopen(yt_base) as response:
            check(b'YT Streamer' in response.read(), 'YTStream dashboard is missing from Ingress')
        for asset in ('app.js', 'style.css', 'lucide.min.js', 'oauth-setup.js'):
            with urllib.request.urlopen(yt_base + asset) as response:
                check(response.status == 200, 'YTStream asset is missing: ' + asset)
        with urllib.request.urlopen(yt_base + 'api/oauth/client') as response:
            oauth_setup = json.load(response)
            check(oauth_setup['redirect_uri'] == yt_base + 'oauth2callback', 'OAuth redirect escaped Ingress')
        with urllib.request.urlopen(urllib.request.Request(yt_base + 'api/accounts',
                data=json.dumps({'id': 'persistence-fixture', 'label': 'Persistent test account'}).encode(),
                headers={'Content-Type': 'application/json'})) as response:
            check(response.status == 201, 'Cannot configure internal YouTube accounts')
        with urllib.request.urlopen(urllib.request.Request(yt_base + 'api/settings',
                data=json.dumps({'history_days_limit': 17}).encode(),
                headers={'Content-Type': 'application/json'})) as response:
            check(response.status == 200, 'Cannot save internal YouTube settings')
        docker('exec', app, '/opt/ytstream-venv/bin/python', '-c',
            "from pathlib import Path; import json,pickle; from google.oauth2.credentials import Credentials; "
            "p=Path('/data/ytstream/accounts/persistence-fixture'); "
            "p.mkdir(parents=True,exist_ok=True); "
            "(p/'token.pickle').write_bytes(pickle.dumps(Credentials(token='fixture-token',client_id='fixture-client'))); "
            "Path('/data/ytstream/stream_history.json').write_text(json.dumps({'streams':{},'fixture':'persistent-history'}))")
        yt_tests = Path(__file__).resolve().parents[1] / 'tests/test_addon_ytstream.py'
        docker('cp', str(yt_tests), app + ':/tmp/test_addon_ytstream.py')
        docker('exec', app, '/opt/ytstream-venv/bin/python', '/tmp/test_addon_ytstream.py')
        if args.generated_password:
            logs = docker('logs', app)
            password = logs.split('First-run LightNVR login: admin / ', 1)[1].splitlines()[0]
            check(12 <= len(password) <= 31, 'Generated password exceeds the LightNVR INI buffer')

        # Other callers cannot access the Ingress listener even with forged headers.
        forbidden = docker('exec', app, 'curl', '-s', '-o', '/dev/null', '-w', '%{http_code}',
            '-H', 'X-Forwarded-For: 172.30.32.2', 'http://127.0.0.1:8099/')
        check(forbidden == '403', 'Ingress listener accepted a non-Supervisor caller')
        forbidden_yt = docker('exec', app, 'curl', '-s', '-o', '/dev/null', '-w', '%{http_code}',
            'http://127.0.0.1:8099/ytstream/api/accounts')
        check(forbidden_yt == '403', 'YTStream accepted a non-Supervisor Ingress caller')
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

        internal_check = Path(__file__).with_name('check_internal_detector.py')
        docker('cp', str(internal_check), app + ':/tmp/check_internal_detector.py')
        check('INTERNAL_DETECTOR_PASS' in docker('exec', app, '/opt/venv/bin/python',
            '/tmp/check_internal_detector.py', 'initialize'), 'Internal AI inference failed')
        bindings = json.loads(docker('inspect', app))[0]['NetworkSettings']['Ports']
        check('8000/tcp' not in bindings, 'Internal detector port was published')
        check('8081/tcp' not in bindings, 'YTStream port was published')
        loopback = docker('exec', app, '/opt/venv/bin/python', '-c',
            "import socket; s=socket.socket(); print(s.connect_ex((socket.gethostbyname(socket.gethostname()), 8000)))")
        check(loopback != '0', 'Detector is reachable on the container network interface')
        yt_loopback = docker('exec', app, '/opt/ytstream-venv/bin/python', '-c',
            "import socket; s=socket.socket(); print(s.connect_ex((socket.gethostbyname(socket.gethostname()), 8081)))")
        check(yt_loopback != '0', 'YTStream is reachable on the container network interface')

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
        with urllib.request.urlopen(yt_base + 'api/accounts') as response:
            check('persistence-fixture' in [a['id'] for a in json.load(response)['accounts']], 'YouTube accounts were lost on recreation')
        with urllib.request.urlopen(yt_base + 'api/settings') as response:
            check(json.load(response)['history_days_limit'] == 17, 'YouTube settings were lost on recreation')
        check('persistent-history' in docker('exec', app, 'cat', '/data/ytstream/stream_history.json'),
              'YouTube history was lost on recreation')
        token_fixture = docker('exec', app, '/opt/ytstream-venv/bin/python', '-c',
            "import pickle; from pathlib import Path; c=pickle.loads(Path('/data/ytstream/accounts/persistence-fixture/token.pickle').read_bytes()); print(c.token=='fixture-token')")
        check(token_fixture == 'True', 'YouTube credentials were lost on recreation')
        check(docker('exec', app, 'cat', marker) == 'persistent media fixture', 'Media was lost on recreation')
        docker('cp', str(internal_check), app + ':/tmp/check_internal_detector.py')
        check('INTERNAL_DETECTOR_PASS' in docker('exec', app, '/opt/venv/bin/python',
            '/tmp/check_internal_detector.py', 'verify'), 'Face/classification data was lost on recreation')
        config = docker('exec', app, 'cat', '/etc/lightnvr/lightnvr.ini')
        check('retention_days = 3' in config, 'Updated retention was not applied')
        # Recovery must work against the actual pinned binary's password format.
        recovered = 'AddonRecoveredPassword123'
        docker('stop', '--time', '30', app)
        docker('rm', '-v', app)
        (folder / 'data/options.json').write_text(json.dumps({'initial_admin_password': recovered,
            'reset_admin_password': True, 'retention_days': 3, 'log_level': 'warning'}))
        start_app()
        wait_ready(f'http://127.0.0.1:{port(app, 8080)}/login.html')
        try:
            opener.open(ingress + 'api/auth/verify', timeout=15)
            raise AssertionError('Recovery did not revoke the previous admin session')
        except urllib.error.HTTPError as error:
            check(error.code == 401, 'Unexpected response for revoked session')
        jar.clear()
        try:
            login(opener, ingress, password)
            raise AssertionError('Old admin password still works after recovery')
        except urllib.error.HTTPError as error:
            check(error.code == 401, 'Unexpected response for old password')
        login(opener, ingress, recovered)
        check(docker('exec', app, 'cat', marker) == 'persistent media fixture', 'Recovery lost recordings')
        backups = docker('exec', app, 'python3', '-c',
            "from pathlib import Path; print(len(list(Path('/data/database').glob('*before-password-reset*'))))")
        check(backups == '1', 'Recovery did not create exactly one database backup')
        docker('stop', '--time', '30', app)
        docker('rm', '-v', app)
        (folder / 'data/options.json').write_text(json.dumps({'initial_admin_password': 'DifferentUnused123',
            'reset_admin_password': False, 'retention_days': 3, 'log_level': 'warning'}))
        start_app()
        wait_ready(f'http://127.0.0.1:{port(app, 8080)}/login.html')
        jar.clear()
        login(opener, ingress, recovered)
        check(docker('exec', app, 'python3', '-c',
            "from pathlib import Path; print(len(list(Path('/data/database').glob('*before-password-reset*'))))") == '1',
            'Disabled recovery unexpectedly ran again')
        docker('stop', '--time', '30', app)
        docker('rm', '-v', app)
        (folder / 'data/options.json').write_text(json.dumps({'auth_enabled': False,
            'retention_days': 3, 'log_level': 'warning'}))
        start_app()
        direct = f'http://127.0.0.1:{port(app, 8080)}/'
        wait_ready(direct + 'login.html')
        jar.clear()
        for base in (ingress, direct):
            with opener.open(base + 'api/auth/verify', timeout=15) as response:
                auth = json.load(response)
                check(auth['authenticated'] and auth['auth_enabled'] is False, 'Login bypass did not take effect')
            for endpoint in ('api/streams', 'api/settings', 'api/system/info'):
                with opener.open(base + endpoint, timeout=15) as response:
                    check(response.status == 200, 'Unauthenticated API was rejected with login disabled')
        docker('stop', '--time', '30', app)
        docker('rm', '-v', app)
        (folder / 'data/options.json').write_text(json.dumps({'auth_enabled': True,
            'retention_days': 3, 'log_level': 'warning'}))
        start_app()
        wait_ready(f'http://127.0.0.1:{port(app, 8080)}/login.html')
        jar.clear()
        try:
            opener.open(ingress + 'api/auth/verify', timeout=15)
            raise AssertionError('Re-enabling authentication still allows unauthenticated access')
        except urllib.error.HTTPError as error:
            check(error.code == 401, 'Unexpected response with authentication restored')
        login(opener, ingress, recovered)
        check(docker('exec', app, 'cat', marker) == 'persistent media fixture', 'Toggling authentication lost recordings')
        service_prefix = (b'/opt/venv/bin/python\x00-m\x00uvicorn\x00' if args.failure_service == 'detector'
                          else b'/opt/ytstream-venv/bin/python\x00/opt/ytstream/ytstream.py\x00')
        docker('exec', app, '/opt/venv/bin/python', '-c',
            "from pathlib import Path; import os,signal; "
            "pids=[int(p.parent.name) for p in Path('/proc').glob('[0-9]*/cmdline') "
            f"if p.read_bytes().startswith({service_prefix!r})]; "
            "assert len(pids)==1,pids; os.kill(pids[0],signal.SIGTERM)")
        for _ in range(30):
            if not json.loads(docker('inspect', app))[0]['State']['Running']:
                break
            time.sleep(1)
        check(not json.loads(docker('inspect', app))[0]['State']['Running'],
              'Add-on stayed up after an internal service exited')
        success = True
        print(json.dumps({'result': 'PASS', 'checks': ['startup', 'login', 'Supervisor-only Ingress', 'cookie path', 'authenticated API', 'WebSocket upgrade', 'internal object and face inference', 'internal classification training', 'detector loopback only', 'face/classification persistence', 'YTStream Ingress and assets', 'YouTube OAuth callbacks', 'YouTube account/settings persistence', 'YTStream loopback only', args.failure_service + ' exit stops add-on', 'graceful stop', 'recreation persistence', 'admin recovery', 'old credentials rejected', 'admin sessions revoked', 'recovery backup', 'recovery disabled', 'login disabled on Ingress and direct port', 'authentication restored'],
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
