#!/usr/bin/env python3
"""Forward the single APS publish endpoint to the local Sonne service."""

import argparse
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ForwardPublish(BaseHTTPRequestHandler):
    timeout = 10

    def log_message(self, *_):
        pass

    def do_GET(self):
        self.send_error(404)

    def finish_publish(self, status):
        self.send_response(status)
        self.send_header('X-Sonne-Publish-Relay', '1')
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_POST(self):
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
    args = parser.parse_args()
    with ThreadingHTTPServer(('127.0.0.1', args.port), ForwardPublish) as server:
        print(f'APS webhook relay listening on http://127.0.0.1:{server.server_port}/aps/publish', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
