#!/usr/bin/env python3
"""Forward the single APS publish endpoint to the local Sonne service."""

import argparse
import hashlib
import hmac
import json
import os
import re
import shutil
import threading
import time
from pathlib import Path
from tempfile import NamedTemporaryFile
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def mac(token, message):
    return hmac.new(token.encode(), message.encode(), hashlib.sha256).hexdigest()


def publish_routes(runtime, endpoint, shared):
    registry = json.loads((runtime / '.state/revit-hub/registry.json').read_text())
    expires = int(time.time()) + 180
    routes = {}
    for client in registry['clients']:
        token = client['token']
        if not isinstance(token, str) or len(token) < 32:
            continue
        name = mac(token, 'sonne-discovery-v1') + '.json'
        signature = mac(token, f'sonne-route-v1\n{endpoint}\n{expires}')
        routes['sr1-' + mac(token, 'sonne-access-v1\n' + endpoint)] = token
        temporary = None
        try:
            with NamedTemporaryFile(mode='w', dir=shared, delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(dict(url=endpoint, expires=expires, signature=signature), stream)
            os.chmod(temporary, 0o644)
            os.replace(temporary, shared / name)
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)
    return routes


def advertise(server, runtime, endpoint_file):
    settings = runtime / '.state/revit-discovery.json'
    last_error = None
    while True:
        try:
            if settings.exists() and endpoint_file.exists():
                endpoint = endpoint_file.read_text().strip()
                if not re.fullmatch(r'https://[a-z0-9-]+\.trycloudflare\.com/revit/', endpoint):
                    raise ValueError('Invalid managed tunnel address.')
                shared = Path(json.loads(settings.read_text())['directory'])
                server.route_tokens = publish_routes(runtime, endpoint, shared)
                last_error = None
        except (OSError, KeyError, ValueError) as error:
            if str(error) != last_error:
                print(f'Revit discovery unavailable: {error}', flush=True)
                last_error = str(error)
        time.sleep(10)


class ForwardPublish(BaseHTTPRequestHandler):
    timeout = 10

    def log_message(self, *_):
        pass

    def do_GET(self):
        if self.server.revit and re.fullmatch(r'/revit/archives/[a-f0-9]{64}', self.path):
            return self.forward_revit(None)
        self.send_error(404)

    def forward_revit(self, body):
        auth = self.headers.get('Authorization', '')
        if not auth.startswith('Bearer '):
            self.send_error(401)
            return
        if auth.startswith('Bearer sr1-'):
            token = self.server.route_tokens.get(auth[7:])
            if not token:
                return self.send_error(401)
            auth = 'Bearer ' + token
        request = Request('http://127.0.0.1:9081' + self.path[len('/revit'):], data=body,
                          headers={'Authorization': auth, 'Content-Type': 'application/json'})
        try:
            response = build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=75)
        except HTTPError as error:
            response = error
        except (URLError, OSError, HTTPException):
            self.send_error(502)
            return
        with response:
            self.send_response(response.status)
            for header in ('Content-Type', 'Content-Length'):
                if response.headers.get(header):
                    self.send_header(header, response.headers[header])
            self.end_headers()
            shutil.copyfileobj(response, self.wfile)

    def finish_publish(self, status):
        self.send_response(status)
        self.send_header('X-Sonne-Publish-Relay', '1')
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_POST(self):
        if self.server.revit and self.path in ('/revit/register', '/revit/poll', '/revit/result'):
            try:
                size = int(self.headers.get('Content-Length', ''))
            except ValueError:
                return self.send_error(411)
            if not 0 < size <= 256000:
                return self.send_error(413)
            return self.forward_revit(self.rfile.read(size))
        if self.path != '/aps/publish':
            self.send_error(404)
            return
        signature = self.headers.get('x-adsk-signature')
        if not signature:
            self.finish_publish(401)
            return
        try:
            length = int(self.headers.get('Content-Length', ''))
        except ValueError:
            self.send_error(411)
            return
        if not 0 < length <= 65536:
            self.send_error(413)
            return
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                self.send_error(400)
                return
            request = Request(
                'http://127.0.0.1:9082/aps/publish', data=raw,
                headers={'x-adsk-signature': signature, 'Content-Type': 'application/json'}
            )
            with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=8) as response:
                status = response.status
        except HTTPError as error:
            status = error.code
        except (URLError, HTTPException, OSError):
            status = 502
        self.finish_publish(status)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=9083, help='Local port, or 0 to choose a free port.')
    parser.add_argument('--revit', action='store_true')
    parser.add_argument('--runtime', type=Path)
    parser.add_argument('--endpoint-file', type=Path)
    args = parser.parse_args()
    with ThreadingHTTPServer(('127.0.0.1', args.port), ForwardPublish) as server:
        server.revit = args.revit
        server.route_tokens = {}
        if args.revit and args.runtime and args.endpoint_file:
            threading.Thread(target=advertise, args=(server, args.runtime, args.endpoint_file), daemon=True).start()
        print(f'APS webhook relay listening on http://127.0.0.1:{server.server_port}/aps/publish', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
