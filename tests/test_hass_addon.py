import contextlib
import importlib.util
import io
from pathlib import Path
import tempfile
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
        with contextlib.redirect_stdout(io.StringIO()) as output:
            addon.prepare({}, *self.paths)
        self.assertEqual(output.getvalue(), '')

    def test_bad_options_do_not_mutate_configuration(self):
        self.prepare()
        before = (self.paths[0] / 'lightnvr.ini').read_bytes()
        invalid = [
            {'retention_days': True}, {'retention_days': 0}, {'log_level': 'verbose'},
            {'initial_admin_password': 'short'},
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
