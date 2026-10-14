"""Test add-on YouTube routes without creating broadcasts or contacting Google."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

root = Path(__file__).resolve().parents[1]
source = root / 'addons/lightnvr/ytstream' if (root / 'addons').exists() else root / 'lightnvr/ytstream'
if source.exists():
    sys.path.insert(0, str(source))
else:
    sys.path.insert(0, '/opt/ytstream')
import ytstream


class AddonYouTubeTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        env = patch.dict(os.environ, {'YTSTREAM_CONFIG_DIR': folder.name, 'LIGHTNVR_RTSP_URL': 'rtsp://127.0.0.1:8554'})
        env.start()
        self.addCleanup(env.stop)
        self.client = ytstream.app.test_client()
        self.headers = {'X-Forwarded-Proto': 'https', 'X-Forwarded-Prefix': '/api/hassio_ingress/fixture/ytstream', 'Host': 'ha.example.test'}
        self.oauth = {'web': {'client_id': 'fixture.apps.googleusercontent.com', 'client_secret': 'fixture-only-secret',
            'auth_uri': 'https://accounts.google.com/o/oauth2/auth', 'token_uri': 'https://oauth2.googleapis.com/token',
            'redirect_uris': ['https://ha.example.test/api/hassio_ingress/fixture/ytstream/oauth2callback']}}

    def test_setup_and_callback_use_ingress_origin_and_prefix(self):
        response = self.client.get('/api/oauth/client', headers=self.headers)
        self.assertFalse(response.json['configured'])
        self.assertEqual(response.json['redirect_uri'], self.oauth['web']['redirect_uris'][0])
        self.assertEqual(self.client.post('/api/oauth/client', json=self.oauth, headers=self.headers).status_code, 200)
        self.assertNotIn('fixture-only-secret', self.client.get('/api/oauth/client', headers=self.headers).get_data(as_text=True))
        flow = MagicMock()
        flow.authorization_url.return_value = ('https://accounts.google.com/authorize-fixture', 'state-fixture')
        with patch.object(ytstream.Flow, 'from_client_secrets_file', return_value=flow) as factory:
            response = self.client.get('/login', headers=self.headers)
            self.assertEqual(response.status_code, 302)
            self.assertEqual(factory.call_args.kwargs['redirect_uri'], self.oauth['web']['redirect_uris'][0])
            with patch.object(ytstream, 'save_credentials'), patch.object(ytstream, 'get_retry_scope', return_value={'project': 'p', 'account': 'a'}):
                response = self.client.get('/oauth2callback?state=state-fixture&code=fixture', headers=self.headers)
                self.assertEqual(response.location, '/api/hassio_ingress/fixture/ytstream/')
                self.assertTrue(flow.fetch_token.call_args.kwargs['authorization_response'].startswith(self.oauth['web']['redirect_uris'][0]))

    def test_reject_invalid_client_and_oauth_state(self):
        for body in ({}, {'installed': self.oauth['web']}, {'web': {**self.oauth['web'], 'token_uri': 'https://invalid.test'}}):
            self.assertEqual(self.client.post('/api/oauth/client', json=body).status_code, 400)
        self.assertFalse((self.folder / 'oauth.json').exists())
        self.assertEqual(self.client.get('/oauth2callback?state=invalid').status_code, 400)

    def test_sync_uses_local_rtsp_and_retains_account_assignment(self):
        (self.folder / 'stream.json').write_text(json.dumps([{'stream_name': 'front', 'url': 'rtsp://old', 'account_id': 'default', 'watch_url': 'saved'}]))
        with patch.object(ytstream, 'start_stream_worker') as worker:
            worker.return_value = None
            response = self.client.post('/api/sync', json=[{'name': 'front', 'ytstream_enabled': True}])
            self.assertEqual(response.status_code, 200)
            worker.assert_called_once_with('rtsp://127.0.0.1:8554/front', 'front', ytstream.sync_playlist_name)
        stream = ytstream.get_streams_from_file()[0]
        self.assertEqual(stream['watch_url'], 'saved')
        self.assertEqual(stream['account_id'], 'default')
        self.assertEqual(stream['url'], 'rtsp://127.0.0.1:8554/front')


if __name__ == '__main__':
    unittest.main()
