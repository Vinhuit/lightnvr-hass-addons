"""Prepare Supervisor volumes without overwriting user-managed NVR settings."""
import contextlib
import ipaddress
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import tempfile
import time

LEVELS = {"error": 0, "warning": 1, "info": 2, "debug": 3}


def validate(options):
    if not isinstance(options, dict):
        raise ValueError("Options must be an object")
    retention = options.get("retention_days", 30)
    if type(retention) is not int or not 1 <= retention <= 3650:
        raise ValueError("retention_days must be between 1 and 3650")
    if options.get("log_level", "info") not in LEVELS:
        raise ValueError("Invalid log_level")
    if type(options.get('auth_enabled', True)) is not bool:
        raise ValueError('auth_enabled must be a boolean')
    for name in ("initial_admin_password", "onvif_network", "webrtc_external_ip"):
        value = options.get(name, "")
        if not isinstance(value, str) or any(c in value for c in "\r\n;#\x00"):
            raise ValueError(f"Invalid {name}: line breaks, INI comment characters and NUL are not supported")
    password = options.get("initial_admin_password", "")
    if password and (len(password) < 12 or len(password.encode('utf-8')) > 31 or re.search(r"\s", password)):
        raise ValueError("initial_admin_password must contain 12–31 characters without whitespace (maximum 31 UTF-8 bytes)")
    if type(options.get('reset_admin_password', False)) is not bool:
        raise ValueError('reset_admin_password must be a boolean')
    if options.get('reset_admin_password') and not password:
        raise ValueError('Set initial_admin_password before enabling reset_admin_password')
    if options.get("onvif_network"):
        network = ipaddress.ip_network(options["onvif_network"], strict=False)
        if network.version != 4:
            raise ValueError("onvif_network must be an IPv4 CIDR network")
    if options.get("webrtc_external_ip"):
        ipaddress.ip_address(options["webrtc_external_ip"])


def patch_ini(text, values):
    """Edit only managed keys, retaining stream definitions and other settings."""
    remaining = {section: dict(keys) for section, keys in values.items()}
    lines = []
    section = None

    def finish_section():
        if section in remaining:
            lines.extend(f"{key} = {value}\n" for key, value in remaining.pop(section).items())

    for line in text.splitlines(keepends=True):
        header = re.match(r"^\s*\[([^]]+)\]", line)
        if header:
            finish_section()
            section = header.group(1)
        key = re.match(r"^\s*([^;#\s=]+)\s*=", line)
        if key and section in values and key.group(1) in values[section]:
            name = key.group(1)
            # Drop duplicate managed keys as well: inih otherwise uses the last.
            if name in remaining.get(section, {}):
                lines.append(f"{name} = {remaining[section].pop(name)}\n")
            continue
        lines.append(line if line.endswith("\n") else line + "\n")
    finish_section()
    for section, keys in remaining.items():
        lines.append(f"\n[{section}]\n")
        lines.extend(f"{key} = {value}\n" for key, value in keys.items())
    return "".join(lines)


def reset_admin(database, password):
    """Recover the existing admin before LightNVR starts, retaining all NVR data."""
    if not database.exists():
        print('Admin password recovery requested: new database; admin will be created with initial_admin_password.', flush=True)
        return  # LightNVR will create admin using the newly written INI password.
    with contextlib.closing(sqlite3.connect(database.as_uri() + '?mode=rw', uri=True)) as db:
        user = db.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()
        if not user:
            raise ValueError('Cannot reset password: existing database has no admin user')
        backup = database.with_name('lightnvr.before-password-reset-' + str(time.time_ns()) + '.db')
        # SQLite backup includes WAL contents and retains cameras, users and recordings.
        with contextlib.closing(sqlite3.connect(backup)) as saved:
            db.backup(saved)
        os.chmod(backup, 0o600)
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 10000, dklen=32)
        with db:
            db.execute('UPDATE users SET password_hash = ?, salt = ?, updated_at = ? WHERE id = ?',
                       (digest.hex(), salt.hex(), int(time.time()), user[0]))
            db.execute('DELETE FROM sessions WHERE user_id = ?', (user[0],))
    print('Admin password reset successfully; clear reset_admin_password after signing in.', flush=True)


def prepare(options, config_dir=Path('/etc/lightnvr'), data_dir=Path('/data'), media_dir=Path('/media')):
    validate(options)
    config_dir, data_dir, media_dir = map(Path, (config_dir, data_dir, media_dir))
    recordings = media_dir / 'lightnvr' / 'recordings'
    for path in (config_dir / 'go2rtc', data_dir / 'database', data_dir / 'models', data_dir / 'detector', data_dir / 'ytstream', data_dir / 'hls', data_dir / 'log', recordings / 'mp4'):
        path.mkdir(parents=True, exist_ok=True)
    ytstream_dir = data_dir / 'ytstream'
    os.chmod(ytstream_dir, 0o700)
    secret = ytstream_dir / 'flask_secret'
    if not secret.exists():
        fd = os.open(secret, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as f:
            f.write(secrets.token_urlsafe(48))
    stream_list = ytstream_dir / 'stream.json'
    if not stream_list.exists():
        stream_list.write_text('[]', encoding='utf-8')
    config = config_dir / 'lightnvr.ini'
    new = not config.exists()
    values = {
        'general': {'pid_file': '/run/lightnvr.pid', 'log_file': str(data_dir / 'log/lightnvr.log'), 'log_level': LEVELS[options.get('log_level', 'info')]},
        'storage': {'path': str(recordings), 'path_hls': str(data_dir / 'hls'), 'mp4_path': str(recordings / 'mp4'), 'retention_days': options.get('retention_days', 30), 'mp4_retention_days': options.get('retention_days', 30), 'ytstream_api_url': 'http://127.0.0.1:8081/api/sync'},
        'database': {'path': str(data_dir / 'database/lightnvr.db')},
        'models': {'path': str(data_dir / 'models')},
        'api_detection': {
            'url': 'http://127.0.0.1:8000/api/v1/detect',
            'backend': 'onnx',
            'face_recognition_api_url': 'http://127.0.0.1:8000/api/v1/faces/recognize',
            'classification_worker_url': 'http://127.0.0.1:8000/api/v1/classification',
        },
        'web': {'port': 8080, 'bind_ip': '0.0.0.0', 'root': '/var/lib/lightnvr/www', 'auth_enabled': str(options.get('auth_enabled', True)).lower()},
        'go2rtc': {'enabled': 'true', 'binary_path': '/bin/go2rtc', 'config_dir': str(config_dir / 'go2rtc'), 'api_port': 1984, 'rtsp_port': 8554, 'webrtc_listen_port': 8555},
    }
    if new or options.get('reset_admin_password'):
        password = options.get('initial_admin_password') or secrets.token_urlsafe(22)
        values['web'].update(username='admin', password=password)
    if new:
        values['storage'].update(auto_delete_oldest='true', record_mp4_directly='true', mp4_segment_duration=900)
        values['memory'] = {'use_swap': 'false'}
        values['go2rtc']['webrtc_enabled'] = 'true'
    if options.get('webrtc_external_ip'):
        values['go2rtc']['external_ip'] = options['webrtc_external_ip']
    if options.get('onvif_network'):
        values['onvif'] = {'discovery_network': options['onvif_network'], 'discovery_enabled': 'true'}
    text = patch_ini(config.read_text() if config.exists() else '; LightNVR Home Assistant configuration\n', values)
    temp = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=config_dir, delete=False) as f:
            temp = f.name
            f.write(text)
        os.chmod(temp, 0o600)
        os.replace(temp, config)
    finally:
        if temp and os.path.exists(temp):
            os.unlink(temp)
    if options.get('reset_admin_password'):
        reset_admin((data_dir / 'database/lightnvr.db').resolve(), password)
    if new and not options.get('initial_admin_password'):
        print(f"First-run LightNVR login: admin / {password}", flush=True)
        print("Change this generated password in LightNVR Users after signing in.", flush=True)
    return new


if __name__ == '__main__':
    try:
        options_path = Path('/data/options.json')
        prepare(json.loads(options_path.read_text()) if options_path.exists() else {})
    except (ValueError, OSError, sqlite3.Error) as error:
        raise SystemExit(f"LightNVR add-on configuration error: {error}")
