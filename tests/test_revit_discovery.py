"""Exercise the Python publisher and the Windows client's JavaScript reader together."""
import json
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
import runpy
import subprocess
import tempfile
from threading import Thread
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
relay = runpy.run_path(str(ROOT / 'scripts/forward-revit-aps.py'))


class DiscoveryTests(unittest.TestCase):
    def test_relay_forwards_scoped_credential_only_to_the_local_hub(self):
        handler = relay['ForwardPublish']
        with ThreadingHTTPServer(('127.0.0.1', 0), handler) as server:
            server.revit = True
            server.route_tokens = {'sr1-fixture': 'private-client-token'}
            worker = Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                with patch.dict(handler.forward_revit.__globals__, build_opener=unittest.mock.Mock()) as scope:
                    response = BytesIO(b'{}')
                    response.status, response.headers = 200, {'Content-Length': '2'}
                    upstream = scope['build_opener'].return_value.open
                    upstream.return_value = response
                    for key, expected in [('stale', 401), ('fixture', 200)]:
                        conn = HTTPConnection(*server.server_address, timeout=3)
                        conn.request('POST', '/revit/register', b'{}', {'Authorization': 'Bearer sr1-' + key})
                        self.assertEqual(conn.getresponse().status, expected)
                        conn.close()
                    upstream.assert_called_once()
                    request = upstream.call_args.args[0]
                    self.assertEqual(request.full_url, 'http://127.0.0.1:9081/register')
                    self.assertEqual(request.get_header('Authorization'), 'Bearer private-client-token')
            finally:
                server.shutdown()
                worker.join()

    def test_restart_and_invalid_announcements(self):
        token = 'test-enrollment-token-' * 3
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hub = root / '.state/revit-hub'
            hub.mkdir(parents=True)
            (hub / 'registry.json').write_text(json.dumps({'clients': [{'token': token}]}))
            share = root / 'discovery'
            share.mkdir()
            reader = """import {discoverRouter} from './integrations/revit/discovery.mjs';
                        console.log(JSON.stringify(await discoverRouter(process.argv[1], process.argv[2])));"""
            def read():
                return subprocess.run(['node', '--input-type=module', '-e', reader, str(share), token],
                                      cwd=ROOT, capture_output=True, text=True)
            for host in ('first', 'after-reboot'):
                url = f'https://{host}.trycloudflare.com/revit/'
                routes = relay['publish_routes'](root, url, share)
                response = read()
                self.assertEqual(response.returncode, 0, response.stderr)
                discovered = json.loads(response.stdout)
                self.assertEqual(discovered['url'], url)
                self.assertEqual(routes[discovered['token']], token)
                file, = share.glob('*.json')
                self.assertNotIn(token, file.read_text())
                self.assertNotIn(discovered['token'], file.read_text())
            record = json.loads(file.read_text())
            file.write_text(json.dumps({**record, 'url': 'https://other.trycloudflare.com/revit/'}))
            self.assertNotEqual(read().returncode, 0)
            with patch('time.time', return_value=1):
                relay['publish_routes'](root, url, share)
            self.assertNotEqual(read().returncode, 0)
