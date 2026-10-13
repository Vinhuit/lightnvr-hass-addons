import contextlib
import importlib.util
import io
import hashlib
from pathlib import Path
import tempfile
import sqlite3
import unittest

ROOT = Path(__file__).resolve().parents[1]
ADDON = ROOT / 'addons/lightnvr' if (ROOT / 'addons/lightnvr').exists() else ROOT / 'lightnvr'
spec = importlib.util.spec_from_file_location('addon_configure', ADDON / 'configure.py')
addon = importlib.util.module_from_spec(spec)
spec.loader.exec_module(addon)


class AddonConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.paths = [self.directory / name for name in ('config', 'data', 'media')]
        self.options = {'retention_days': 30, 'log_level': 'info', 'initial_admin_password': 'TestPassword1234'}

    def prepare(self, options=None):
        return addon.prepare(options or self.options, *self.paths)

    def test_first_start_creates_persistent_paths_and_authentication(self):
        self.assertTrue(self.prepare())
        text = (self.paths[0] / 'lightnvr.ini').read_text()
        self.assertIn('password = TestPassword1234', text)
        self.assertIn('auth_enabled = true', text)
        self.assertIn(str(self.paths[1] / 'database/lightnvr.db'), text)
        self.assertIn(str(self.paths[2] / 'lightnvr/recordings'), text)
        self.assertTrue((self.paths[2] / 'lightnvr/recordings/mp4').is_dir())

    def test_restart_keeps_password_cameras_and_custom_settings(self):
        self.prepare()
        config = self.paths[0] / 'lightnvr.ini'
        config.write_text(config.read_text() + '\n[mqtt]\nbroker_host = core-mosquitto\n\n[stream.front]\nurl = rtsp://user:pass@camera/live\n')
        options = {**self.options, 'initial_admin_password': 'DifferentPassword123', 'retention_days': 7}
        self.assertFalse(self.prepare(options))
        text = config.read_text()
        self.assertIn('password = TestPassword1234', text)
        self.assertIn('retention_days = 7', text)
        self.assertIn('url = rtsp://user:pass@camera/live', text)
        self.assertIn('broker_host = core-mosquitto', text)
        self.assertNotIn('DifferentPassword', text)

    def test_generated_password_is_printed_only_once(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            addon.prepare({}, *self.paths)
        self.assertIn('First-run LightNVR login', output.getvalue())
        generated = output.getvalue().split('admin / ', 1)[1].splitlines()[0]
        self.assertGreaterEqual(len(generated), 12)
        self.assertLessEqual(len(generated), 31)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            addon.prepare({}, *self.paths)
        self.assertEqual(output.getvalue(), '')

    def test_bad_options_do_not_mutate_configuration(self):
        self.prepare()
        before = (self.paths[0] / 'lightnvr.ini').read_bytes()
        invalid = [
            {'retention_days': True}, {'retention_days': 0}, {'log_level': 'verbose'},
            {'initial_admin_password': 'short'},
            {'initial_admin_password': 'a' * 32},
            {'initial_admin_password': '\u00e1' * 20},
            {'reset_admin_password': 'true'},
            {'reset_admin_password': True, 'initial_admin_password': ''},
            {'initial_admin_password': 'password\n[web]\nauth_enabled=false'},
            {'detection_server': 'file:///etc/passwd'},
            {'ytstream_server': 'file:///etc/passwd'},
            {'onvif_network': 'not-a-network'}, {'webrtc_external_ip': 'host.invalid'},
        ]
        for change in invalid:
            with self.subTest(change=change):
                with self.assertRaises(ValueError):
                    self.prepare({**self.options, **change})
                self.assertEqual((self.paths[0] / 'lightnvr.ini').read_bytes(), before)

    def test_optional_services_are_saved_and_preserved_when_omitted(self):
        self.prepare({**self.options, 'detection_server': 'http://detector:8000/detect', 'onvif_network': '192.168.1.0/24', 'webrtc_external_ip': '192.168.1.20'})
        self.prepare()
        text = (self.paths[0] / 'lightnvr.ini').read_text()
        for value in ['http://detector:8000/detect', '192.168.1.0/24', '192.168.1.20']:
            self.assertIn(value, text)

    def test_duplicate_managed_keys_cannot_override_auth_or_paths(self):
        text = addon.patch_ini('[web]\nauth_enabled = false\nauth_enabled = false\n; keep comment', {'web': {'auth_enabled': 'true'}})
        self.assertEqual(text.count('auth_enabled = true'), 1)
        self.assertNotIn('auth_enabled = false', text)
        self.assertIn('; keep comment', text)

    def test_youtube_sync_requires_an_explicit_external_service(self):
        self.prepare()
        config = self.paths[0] / 'lightnvr.ini'
        self.assertIn('ytstream_api_url = \n', config.read_text())
        self.prepare({**self.options, 'ytstream_server': 'http://ytstream:8081/api/sync'})
        self.assertIn('ytstream_api_url = http://ytstream:8081/api/sync', config.read_text())
        self.prepare()
        self.assertNotIn('http://ytstream', config.read_text())

    def test_recovery_resets_existing_admin_and_preserves_other_data(self):
        self.prepare()
        database = self.paths[1] / 'database/lightnvr.db'
        with sqlite3.connect(database) as db:
            db.executescript("""
                CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, password_hash TEXT, salt TEXT, updated_at INTEGER);
                INSERT INTO users VALUES (1, 'admin', 'oldhash', 'oldsalt', 1);
                INSERT INTO users VALUES (2, 'viewer', 'viewerhash', 'viewersalt', 1);
                CREATE TABLE sessions (user_id INTEGER, token TEXT);
                INSERT INTO sessions VALUES (1, 'admin-token'), (2, 'viewer-token');
                CREATE TABLE streams (name TEXT);
                INSERT INTO streams VALUES ('front');
            """)
        options = {**self.options, 'initial_admin_password': 'RecoveredPassword123', 'reset_admin_password': True}
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.prepare(options)
        self.assertIn('Admin password reset successfully', output.getvalue())
        self.assertNotIn(options['initial_admin_password'], output.getvalue())
        with sqlite3.connect(database) as db:
            digest, salt = db.execute('SELECT password_hash, salt FROM users WHERE id = 1').fetchone()
            expected = hashlib.pbkdf2_hmac('sha256', b'RecoveredPassword123', bytes.fromhex(salt), 10000, dklen=32).hex()
            self.assertEqual(digest, expected)
            self.assertEqual(db.execute('SELECT token FROM sessions').fetchall(), [('viewer-token',)])
            self.assertEqual(db.execute('SELECT password_hash FROM users WHERE id = 2').fetchone()[0], 'viewerhash')
            self.assertEqual(db.execute('SELECT name FROM streams').fetchone()[0], 'front')
        backup = next(database.parent.glob('lightnvr.before-password-reset-*.db'))
        with sqlite3.connect(backup) as db:
            self.assertEqual(db.execute('SELECT password_hash FROM users WHERE id = 1').fetchone()[0], 'oldhash')
        self.prepare()
        with sqlite3.connect(database) as db:
            self.assertEqual(db.execute('SELECT password_hash FROM users WHERE id = 1').fetchone()[0], expected)

    def test_recovery_without_database_sets_initial_password(self):
        self.prepare()
        self.prepare({**self.options, 'initial_admin_password': 'RecoveredPassword123', 'reset_admin_password': True})
        self.assertIn('password = RecoveredPassword123', (self.paths[0] / 'lightnvr.ini').read_text())
        self.assertFalse((self.paths[1] / 'database/lightnvr.db').exists())

    def test_recovery_without_admin_does_not_change_database(self):
        self.prepare()
        database = self.paths[1] / 'database/lightnvr.db'
        with sqlite3.connect(database) as db:
            db.execute('CREATE TABLE users (id INTEGER, username TEXT)')
            db.execute("INSERT INTO users VALUES (2, 'viewer')")
        with self.assertRaisesRegex(ValueError, 'no admin user'):
            self.prepare({**self.options, 'reset_admin_password': True})
        with sqlite3.connect(database) as db:
            self.assertEqual(db.execute('SELECT * FROM users').fetchall(), [(2, 'viewer')])
        self.assertEqual(list(database.parent.glob('*before-password-reset*')), [])

    def test_repository_metadata_matches_runtime_and_translations(self):
        import yaml
        folder = ADDON
        config = yaml.safe_load((folder / 'config.yaml').read_text())
        repository = yaml.safe_load((ROOT / 'repository.yaml').read_text())
        self.assertEqual(repository['url'], config['url'])
        self.assertEqual(config['arch'], ['amd64'])
        self.assertTrue(config['ingress'])
        self.assertEqual(config['ingress_port'], 8099)
        self.assertEqual(config['backup'], 'cold')
        self.assertEqual(config['map'][0]['type'], 'app_config')
        self.assertIsNone(config['ports']['8080/tcp'])
        self.assertNotIn('8099/tcp', config['ports'])
        self.assertNotIn('image', config)
        for language in ('en', 'vi'):
            translation = yaml.safe_load((folder / f'translations/{language}.yaml').read_text(encoding='utf-8'))
            self.assertEqual(set(translation['configuration']), set(config['schema']))
            self.assertEqual(set(translation['network']), set(config['ports']))


if __name__ == '__main__':
    unittest.main()
