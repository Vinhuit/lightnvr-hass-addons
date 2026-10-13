"""Prepare Supervisor volumes without overwriting user-managed NVR settings."""
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
from urllib.parse import urlparse

LEVELS = {"error": 0, "warning": 1, "info": 2, "debug": 3}


def validate(options):
    if not isinstance(options, dict):
        raise ValueError("Options must be an object")
    retention = options.get("retention_days", 30)
    if type(retention) is not int or not 1 <= retention <= 3650:
        raise ValueError("retention_days must be between 1 and 3650")
    if options.get("log_level", "info") not in LEVELS:
        raise ValueError("Invalid log_level")
    for name in ("initial_admin_password", "detection_server", "ytstream_server", "onvif_network", "webrtc_external_ip"):
        value = options.get(name, "")
        if not isinstance(value, str) or any(c in value for c in "\r\n;#\x00"):
            raise ValueError(f"Invalid {name}: line breaks, INI comment characters and NUL are not supported")
    password = options.get("initial_admin_password", "")
    if password and (len(password) < 12 or len(password) > 63 or re.search(r"\s", password)):
        raise ValueError("initial_admin_password must contain 12–63 characters without whitespace")
    if options.get("onvif_network"):
        network = ipaddress.ip_network(options["onvif_network"], strict=False)
        if network.version != 4:
            raise ValueError("onvif_network must be an IPv4 CIDR network")
    if options.get("webrtc_external_ip"):
        ipaddress.ip_address(options["webrtc_external_ip"])
    for name in ('detection_server', 'ytstream_server'):
        if options.get(name):
            url = urlparse(options[name])
            if url.scheme not in ("http", "https") or not url.hostname:
                raise ValueError(f"{name} must be an HTTP(S) URL")


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


def prepare(options, config_dir=Path('/etc/lightnvr'), data_dir=Path('/data'), media_dir=Path('/media')):
    validate(options)
    config_dir, data_dir, media_dir = map(Path, (config_dir, data_dir, media_dir))
    recordings = media_dir / 'lightnvr' / 'recordings'
    for path in (config_dir / 'go2rtc', data_dir / 'database', data_dir / 'models', data_dir / 'hls', data_dir / 'log', recordings / 'mp4'):
        path.mkdir(parents=True, exist_ok=True)
    config = config_dir / 'lightnvr.ini'
    new = not config.exists()
    values = {
        'general': {'pid_file': '/run/lightnvr.pid', 'log_file': str(data_dir / 'log/lightnvr.log'), 'log_level': LEVELS[options.get('log_level', 'info')]},
        'storage': {'path': str(recordings), 'path_hls': str(data_dir / 'hls'), 'mp4_path': str(recordings / 'mp4'), 'retention_days': options.get('retention_days', 30), 'mp4_retention_days': options.get('retention_days', 30), 'ytstream_api_url': options.get('ytstream_server', '')},
        'database': {'path': str(data_dir / 'database/lightnvr.db')},
        'models': {'path': str(data_dir / 'models')},
        'web': {'port': 8080, 'bind_ip': '0.0.0.0', 'root': '/var/lib/lightnvr/www', 'auth_enabled': 'true'},
        'go2rtc': {'enabled': 'true', 'binary_path': '/bin/go2rtc', 'config_dir': str(config_dir / 'go2rtc'), 'api_port': 1984, 'rtsp_port': 8554, 'webrtc_listen_port': 8555},
    }
    if new:
        password = options.get('initial_admin_password') or secrets.token_urlsafe(24)
        values['web'].update(username='admin', password=password)
        values['storage'].update(auto_delete_oldest='true', record_mp4_directly='true', mp4_segment_duration=900)
        values['memory'] = {'use_swap': 'false'}
        values['go2rtc']['webrtc_enabled'] = 'true'
    if options.get('detection_server'):
        values['api_detection'] = {'url': options['detection_server']}
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
    if new and not options.get('initial_admin_password'):
        print(f"First-run LightNVR login: admin / {password}", flush=True)
        print("Change this generated password in LightNVR Users after signing in.", flush=True)
    return new


if __name__ == '__main__':
    try:
        options_path = Path('/data/options.json')
        prepare(json.loads(options_path.read_text()) if options_path.exists() else {})
    except (ValueError, OSError) as error:
        raise SystemExit(f"LightNVR add-on configuration error: {error}")
