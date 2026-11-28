#!/usr/bin/env python3
"""Configure local Revit MCP access or the Autodesk publication integration."""

import argparse
import base64
import fcntl
from contextlib import contextmanager
from dataclasses import dataclass
from errno import EADDRINUSE
from getpass import GetPassWarning, getpass
import hashlib
import importlib.util
import hmac
import json
import os
from pathlib import Path
import platform
import pwd
import re
import shlex
import secrets
import signal
import shutil
import subprocess
import sys
from tempfile import NamedTemporaryFile, TemporaryDirectory
import time
import warnings
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener, urlopen


APS = 'https://developer.api.autodesk.com'
CALLBACK = 'http://localhost:8765/aps/oauth/callback'
HOOK_PATH = '/webhooks/v1/systems/adsk.c4r/events/model.publish/hooks'
UUID = r'[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}'
PROJECT = re.compile(r'(?:b\.)?(' + UUID + r')')
LINEAGE = re.compile(r'urn:adsk\.wip(?:prod|emea):dm\.lineage:[\w-]{1,200}', re.ASCII)
FOLDER = re.compile(r'urn:adsk\.wip(?:prod|emea):fs\.folder:co\.[\w-]{1,200}', re.ASCII)
REGIONS = ('US', 'EMEA', 'AUS', 'CAN', 'DEU', 'IND', 'JPN', 'GBR')
CLOUD_VERSION = 'versions:autodesk.bim360:C4RModel'
RELAY_ORIGIN = re.compile(r'APS webhook relay listening on (http://127\.0\.0\.1:\d+)/aps/publish')
TUNNEL_ORIGIN = re.compile(r'(https://[a-z0-9-]+\.trycloudflare\.com)(?=[\s|/]|$)')
# https://github.com/cloudflare/cloudflared/releases/tag/2026.11.1
CLOUDFLARED_VERSION = '2026.11.1'
CLOUDFLARED_ASSETS = {
    'x86_64': ('cloudflared-linux-amd64', '97bb3b02d23a5980fc9c15a1a270e692da1923f103f920d5b16d4931d5ab55e7'),
    'aarch64': ('cloudflared-linux-arm64', 'b155cec3e48e489cfb8e5c602769fb8c771083018d8881d68b4d18a791567c5a'),
}
DOWNLOAD_LIMIT = 64 * 1024 * 1024
DOWNLOAD_CHUNK = 1024 * 1024


class SetupError(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def aps_request(path, *, label, bearer=None, credentials=None, form=None, body=None, region=None):
    url = urljoin(APS, path)
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.netloc != 'developer.api.autodesk.com':
        raise SetupError('APS supplied an unexpected URL.')
    headers = {'Accept': 'application/json'}
    if credentials:
        value = ':'.join(credentials).encode()
        headers['Authorization'] = 'Basic ' + base64.b64encode(value).decode()
    if bearer:
        headers['Authorization'] = 'Bearer ' + bearer
    if region:
        headers['region'] = region
    data = None
    if form is not None:
        headers['Content-Type'] = 'application/x-www-form-urlencoded'
        data = urlencode(form).encode()
    if body is not None:
        headers['Content-Type'] = 'application/json'
        data = json.dumps(body).encode()
    try:
        with build_opener(NoRedirect()).open(Request(url, data, headers), timeout=25) as response:
            raw = response.read()
            return (json.loads(raw) if raw else {}), response.headers
    except HTTPError as error:
        raise SetupError(f'{label} returned HTTP {error.code}.') from None
    except (URLError, TimeoutError, ValueError):
        raise SetupError(f'{label} could not be completed.') from None


def authorize(credentials):
    state = secrets.token_urlsafe(32)
    result = {}

    class CallbackHandler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query)
            supplied = query.get('state', [''])[0]
            if parsed.path != '/aps/oauth/callback' or not hmac.compare_digest(supplied.encode(), state.encode()):
                self.send_error(400, 'Unexpected OAuth callback.')
                return
            result['code'] = query.get('code', [''])[0]
            message = b'Return to the setup terminal. You can close this page.'
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.end_headers()
            self.wfile.write(message)

    with HTTPServer(('127.0.0.1', 8765), CallbackHandler) as server:
        url = APS + '/authentication/v2/authorize?' + urlencode({
            'client_id': credentials[0], 'response_type': 'code', 'redirect_uri': CALLBACK,
            'scope': 'data:read', 'state': state
        })
        print('Open this URL in your browser and sign in to Autodesk:\n' + url, flush=True)
        deadline = time.monotonic() + 600
        server.timeout = 1
        while 'code' not in result and time.monotonic() < deadline:
            server.handle_request()
    if not result.get('code'):
        raise SetupError('Autodesk authorization was cancelled or the callback timed out.')
    tokens, _ = aps_request('/authentication/v2/token', label='APS authorization code exchange',
                            credentials=credentials, form={
                                'grant_type': 'authorization_code', 'code': result['code'],
                                'redirect_uri': CALLBACK
                            })
    if not tokens.get('access_token') or not tokens.get('refresh_token'):
        raise SetupError('APS did not supply both OAuth tokens.')
    return tokens


@dataclass(frozen=True)
class Model:
    project: str
    lineage: str
    folder: str
    name: str
    first_version: int


def model_ids(link):
    url = urlsplit(link)
    if url.scheme != 'https' or not ((url.hostname or '').endswith('.autodesk.com') or url.hostname == 'acc.autodesk.eu'):
        raise SetupError('Use an Autodesk Forma/ACC browser URL for the model or its folder.')
    query = parse_qs(url.query)
    project = re.search(r'/projects/(' + UUID + r')(?=/|$)', url.path)
    return (
        project[1] if project else query.get('projectId', [''])[0],
        query.get('folderUrn', [''])[0],
        query.get('entityId', [''])[0]
    )


def choose_model(link, token):
    project, folder, lineage = model_ids(link)
    match = PROJECT.fullmatch(project or input('ACC project ID: ').strip())
    if not match:
        raise SetupError('An ACC project UUID is required.')
    project = 'b.' + match[1]
    base = '/data/v1/projects/' + project
    if LINEAGE.fullmatch(lineage):
        item, _ = aps_request(base + '/items/' + quote(lineage, safe=''), bearer=token, label='ACC model lookup')
        item = item['data']
        parent, _ = aps_request(base + '/items/' + quote(lineage, safe='') + '/parent', bearer=token,
                                label='ACC parent folder lookup')
        folder = parent['data']['id']
    else:
        raise SetupError('Use a direct model link for a single-model publication check.')
    if not LINEAGE.fullmatch(lineage) or not FOLDER.fullmatch(folder):
        raise SetupError('APS did not return the model lineage and parent folder.')
    name = item['attributes']['displayName']
    if not name.lower().endswith('.rvt'):
        raise SetupError('The selected ACC item is not a Revit model.')
    checked, _ = aps_request(base + '/items/' + quote(lineage, safe='') + '/tip', bearer=token,
                             label='ACC published version lookup')
    tip = checked['data']
    version = tip['attributes']['versionNumber']
    if tip['attributes']['extension']['type'] != CLOUD_VERSION or type(version) is not int or version < 1:
        raise SetupError('Save the test RVT from Revit as a cloud-workshared model and complete its initial publication.')
    path = '/construction/rcm/v1/projects/' + project + '/published-versions/' + quote(tip['id'], safe='') + '/linked-files?limit=1'
    details, _ = aps_request(path, bearer=token, label='ACC published RVT access check')
    host = details.get('hostFile') or {}
    if (host.get('itemId') != lineage or host.get('publishStatus') != 'Published' or
            host.get('versionId') not in (tip['id'], f'{lineage}?version={version}') or
            not host.get('signedUrl')):
        raise SetupError('Wait for the initial ACC publication to finish and verify download access to the model.')
    size = host.get('size')
    if type(size) is not int or not 8 <= size <= 512 * 1024 * 1024:
        raise SetupError('The pilot requires a standalone published RVT of at most 512 MiB.')
    return Model(project, lineage, folder, name, version)


def folder_catalog(project, folder, token):
    path = '/data/v1/projects/' + project + '/folders/' + quote(folder, safe='') + '/contents'
    url = path
    seen = set()
    files = {}
    while url:
        if url in seen or unquote(urlsplit(urljoin(APS, url)).path) != unquote(path):
            raise SetupError('Unexpected folder pagination URL.')
        seen.add(url)
        body, _ = aps_request(url, bearer=token, label='ACC folder contents')
        for item in body['data']:
            if item['type'] == 'items':
                files[item['id']] = {'id': item['id'], 'name': item['attributes']['displayName'],
                                     'version_id': item.get('relationships', {}).get('tip', {}).get('data', {}).get('id')}
        url = (body.get('links', {}).get('next') or {}).get('href')
    return sorted(files.values(), key=lambda item: (item['name'].casefold(), item['id']))


def read_document(project, file, token, region):
    suffix = Path(file['name']).suffix.lower()
    if suffix in {'.rvt', '.dwg', '.ifc'} and file['version_id']:
        urn = base64.urlsafe_b64encode(file['version_id'].encode()).decode().rstrip('=')
        path = '/modelderivative/v2/designdata/' + urn + '/metadata'
        views, _ = aps_request(path, bearer=token, region=region, label='ACC model views')
        entries = views['data']['metadata']
        view = next((v for v in entries if v.get('role') == '3d'), next(iter(entries), None))
        if not view:
            raise SetupError('No translated model view available.')
        tree, _ = aps_request(path + '/' + quote(view['guid'], safe=''), bearer=token,
                              region=region, label='ACC model object tree')
        return json.dumps({'view': view.get('name'), 'objects': tree['data']['objects']}, ensure_ascii=False, indent=2)
    if suffix not in {'.txt', '.md', '.csv', '.tsv', '.json', '.xml', '.pdf'}:
        return None
    version = file['version_id']
    if not version:
        raise SetupError('No downloadable version.')
    data, _ = aps_request('/data/v1/projects/' + project + '/versions/' + quote(version, safe=''),
                          label='ACC file version', bearer=token)
    record = data['data']
    if record['id'] != version or record['relationships']['item']['data']['id'] != file['id']:
        raise SetupError('Unexpected ACC file version.')
    urn = record['relationships']['storage']['data']['id']
    match = re.fullmatch(r'urn:adsk.objects:os.object:([^/]+)/(.+)', urn)
    if not match:
        raise SetupError('No downloadable storage object.')
    signed, _ = aps_request('/oss/v2/buckets/' + quote(match[1], safe='') + '/objects/' +
                            quote(match[2], safe='') + '/signeds3download', label='ACC download', bearer=token)
    url = urlsplit(signed.get('url', ''))
    if (url.scheme != 'https' or url.username or url.password or url.port or
            not re.search(r'(?:^|\.)s3(?:[.-][a-z0-9-]+)*\.amazonaws\.com$', url.hostname or '')):
        raise SetupError('No direct S3 download available.')
    limit = 32 * 1024 * 1024
    with build_opener(NoRedirect()).open(url.geturl(), timeout=40) as response:
        raw = response.read(limit + 1)
    if len(raw) > limit:
        raise SetupError('File exceeds the 32 MiB reading limit.')
    if suffix == '.pdf':
        result = subprocess.run(['pdftotext', '-enc', 'UTF-8', '-', '-'], input=raw,
                                capture_output=True, timeout=40, check=True)
        text = result.stdout.decode()
    else:
        encoding = 'utf-16' if raw.startswith((b'\xff\xfe', b'\xfe\xff')) else 'utf-8-sig'
        text = raw.decode(encoding)
    if not text.strip():
        raise SetupError('No extractable text.')
    return text


def sync_documents(workspace, project, files, token, region):
    directory = workspace / 'acc-content'
    if directory.is_symlink():
        raise SetupError('Refusing linked ACC content directory.')
    directory.mkdir(mode=0o700, exist_ok=True)
    retained = set()
    for file in files:
        key = hashlib.sha256(json.dumps([project, file['id'], file['version_id'],
                                         Path(file['name']).suffix.lower()]).encode()).hexdigest()
        target = directory / (key + '.txt')
        try:
            if not target.is_file() or target.is_symlink():
                text = read_document(project, file, token, region)
                if text is None:
                    file['content_status'] = 'unsupported format'
                    continue
                write_private(target, text)
            file['content_path'] = '/workspace/files/acc-content/' + target.name
            retained.add(target.name)
        except (SetupError, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
            file['content_status'] = str(error) if isinstance(error, SetupError) else 'Content unavailable.'
    return [p for p in directory.iterdir() if re.fullmatch(r'[a-f0-9]{64}\.txt', p.name) and p.name not in retained]


def detach_sync():
    session = os.environ.get('SONNE_ACC_SESSION')
    if session:
        subprocess.run(['tmux', '-L', 'sonne-acc', 'detach-client', '-s', session], check=False)


def watch_folder(runtime, project, folder, credentials, tokens, processes, region, saved=None):
    path = runtime / 'workspace/files/acc-folder.json'
    expires = time.monotonic() + int(tokens['expires_in']) - 60
    first = True
    while True:
        if any(p.poll() is not None for p in processes.values()):
            raise SetupError('Tunnel or relay stopped. Rerun setup to reconnect.')
        try:
            if time.monotonic() >= expires:
                renewed, _ = aps_request('/authentication/v2/token', label='ACC folder access renewal',
                                         credentials=credentials, form={'grant_type': 'refresh_token',
                                         'refresh_token': tokens['refresh_token'], 'scope': 'data:read'})
                tokens.update(renewed)
                if saved is not None:
                    saved['refresh_token'] = tokens['refresh_token']
                    write_json(runtime / '.state/acc-startup/folder.json', saved)
                expires = time.monotonic() + int(tokens['expires_in']) - 60
            files = folder_catalog(project, folder, tokens['access_token'])
            stale = sync_documents(path.parent, project, files, tokens['access_token'], region)
            write_json(path, {'project': project, 'folder': folder, 'updated_at': time.time(), 'files': files})
            print(f'ACC folder synced: {len(files)} files in /workspace/files/acc-folder.json', flush=True)
            for old in stale:
                old.unlink(missing_ok=True)
            if first:
                detach_sync()
                first = False
        except (SetupError, OSError) as error:
            if first:
                raise
            print(f'ACC sync failed; retrying in one minute: {error}', flush=True)
        time.sleep(60)


def hook_id(response, headers):
    location = headers.get('Location', '')
    value = response.get('hookId') or (response.get('data') or {}).get('hookId')
    value = value or urlsplit(location.rstrip('/')).path.rsplit('/', 1)[-1]
    if not value or not re.fullmatch(r'[\w-]{1,100}', value, flags=re.ASCII):
        raise SetupError('APS did not return a valid webhook ID.')
    return value


def remove_saved_hook(runtime):
    directory = runtime / '.state/revit-publications-acc'
    config = directory / 'config.json'
    if not config.is_file():
        return
    aps = json.loads(config.read_text())['aps']
    hook = hook_id({'hookId': aps['hook_id']}, {})
    auth = json.loads((directory / 'aps-auth.json').read_text())
    token, _ = aps_request('/authentication/v2/token', label='APS webhook removal authorization',
                           credentials=(auth['client_id'], auth['client_secret']),
                           form={'grant_type': 'client_credentials', 'scope': 'data:read data:write'})
    request = Request(APS + HOOK_PATH + '/' + quote(hook, safe=''), method='DELETE',
                      headers={'Authorization': 'Bearer ' + token['access_token'], 'region': aps['region']})
    try:
        with build_opener(NoRedirect()).open(request, timeout=20):
            pass
    except HTTPError as error:
        if error.code != 404:
            raise SetupError(f'Could not remove APS webhook {hook} (HTTP {error.code}).') from None
    print(f'APS webhook {hook} removed or already absent.')


def write_private(path, contents):
    temporary = path.with_name('.' + path.name + '-' + secrets.token_hex(8))
    try:
        with temporary.open('x', encoding='utf-8') as handle:
            handle.write(contents)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_json(path, value):
    write_private(path, json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def compose_command(runtime, profile='revit-publications'):
    files = ['compose.yaml', 'compose.override.yaml', 'compose.prompt-studio.yaml', 'compose.revit.yaml']
    command = ['docker', 'compose', '--env-file', '.env']
    for name in files:
        if (runtime / name).is_file():
            command.extend(('-f', name))
    return command + ['--profile', profile]


def deploy(runtime, directory):
    source = Path(__file__).resolve().parents[1]
    files = [Path('compose.revit.yaml')]
    files.extend(Path('integrations/revit') / name for name in (
        'Dockerfile', 'hub.mjs', 'common.mjs', 'publications.mjs', 'publications-aps.mjs', 'publication-server.mjs'
    ))
    for path in files:
        if (source / path).resolve() != (runtime / path).resolve():
            shutil.copyfile(source / path, runtime / path)
    compose = compose_command(runtime)
    environment = {**os.environ, 'REVIT_PUBLICATIONS_DIR': str(directory)}
    if environment.get('SONNE_DOCKER_USE_SUDO') == '1':
        compose = ['sudo', 'env', 'REVIT_PUBLICATIONS_DIR=' + str(directory), *compose]
    subprocess.run([*compose, 'up', '-d', '--build', '--wait', '--wait-timeout', '60', 'revit-hub', 'revit-publications'],
                   cwd=runtime, env=environment, check=True)
    # Keep ordinary Compose commands pointed at the selected history on restart.
    env_file = runtime / '.env'
    current_env = env_file.read_text(encoding='utf-8')
    lines = [line for line in current_env.splitlines() if not line.startswith('REVIT_PUBLICATIONS_DIR=')]
    if directory.name != 'revit-publications':
        lines.append('REVIT_PUBLICATIONS_DIR=./.state/' + directory.name)
    contents = '\n'.join(lines) + '\n'
    if contents != current_env:
        write_private(env_file, contents)


def check_callback(url):
    request = Request(url, data=b'{}', headers={'Content-Type': 'application/json'})
    for attempt in range(3):
        try:
            with build_opener(NoRedirect()).open(request, timeout=8):
                pass
        except HTTPError as error:
            if error.code == 401 and error.headers.get('X-Sonne-Publish-Relay') == '1':
                return
        except (URLError, TimeoutError):
            pass
        if attempt < 2:
            time.sleep(2)
    raise SetupError('The HTTPS webhook URL is not reaching Sonne. Check the tunnel and try again.')


@contextmanager
def running_process(command, log_path):
    with log_path.open('wb') as log:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        try:
            yield process
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def wait_for_address(label, process, log_path, pattern, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if (code := process.poll()) is not None:
            raise SetupError(f'{label} exited before it was ready (status {code}).')
        match = pattern.search(log_path.read_text(encoding='utf-8', errors='replace'))
        if match:
            return match[1]
        time.sleep(0.2)
    raise SetupError(f'{label} did not provide an address within {timeout} seconds.')


def binary_matches(path, checksum):
    if not path.is_file() or not os.access(path, os.X_OK) or path.stat().st_size > DOWNLOAD_LIMIT:
        return False
    with path.open('rb') as binary:
        digest = hashlib.sha256()
        for chunk in iter(lambda: binary.read(DOWNLOAD_CHUNK), b''):
            digest.update(chunk)
    return hmac.compare_digest(digest.hexdigest(), checksum)


def resolve_cloudflared(runtime):
    installed = shutil.which('cloudflared')
    if installed:
        return installed
    asset = CLOUDFLARED_ASSETS.get(platform.machine().lower())
    if platform.system() != 'Linux' or asset is None:
        raise SetupError('Automatic cloudflared installation supports x86-64 and ARM64 Linux in WSL.')
    name, checksum = asset
    directory = runtime / '.state/revit-publications-acc/bin'
    target = directory / f'{name}-{CLOUDFLARED_VERSION}'
    temporary = None
    try:
        if binary_matches(target, checksum):
            return str(target)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        print(f'Downloading cloudflared {CLOUDFLARED_VERSION} from Cloudflare’s official release.', flush=True)
        url = f'https://github.com/cloudflare/cloudflared/releases/download/{CLOUDFLARED_VERSION}/{name}'
        with NamedTemporaryFile(dir=directory, prefix='.cloudflared-', delete=False) as output:
            temporary = Path(output.name)
            digest = hashlib.sha256()
            size = 0
            with urlopen(url, timeout=30) as response:
                for chunk in iter(lambda: response.read(DOWNLOAD_CHUNK), b''):
                    size += len(chunk)
                    if size > DOWNLOAD_LIMIT:
                        raise SetupError('The cloudflared download exceeded the expected size.')
                    digest.update(chunk)
                    output.write(chunk)
        if not hmac.compare_digest(digest.hexdigest(), checksum):
            raise SetupError('The cloudflared download failed SHA-256 verification.')
        temporary.chmod(0o700)
        temporary.replace(target)
        return str(target)
    except (OSError, TimeoutError) as error:
        raise SetupError('Could not download or cache cloudflared. Check WSL internet access and Sonne runtime permissions.') from error
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


@contextmanager
def quick_tunnel(runtime, revit=False):
    cloudflared = resolve_cloudflared(runtime)
    relay_script = Path(__file__).with_name('forward-revit-aps.py')
    with TemporaryDirectory(prefix='sonne-acc-') as temp:
        relay_log = Path(temp) / 'relay.log'
        tunnel_log = Path(temp) / 'cloudflared.log'
        tunnel_config = Path(temp) / 'cloudflared.yml'
        tunnel_config.write_text('{}\n', encoding='utf-8')
        print('Starting the local relay and Cloudflare Quick Tunnel.', flush=True)
        endpoint_file = Path(temp) / 'router-url'
        relay_args = ['--revit', '--runtime', str(runtime), '--endpoint-file', str(endpoint_file)] if revit else []
        with running_process([sys.executable, str(relay_script), '--port', '0', *relay_args], relay_log) as relay:
            local = wait_for_address('Webhook relay', relay, relay_log, RELAY_ORIGIN, 10)
            command = [cloudflared, 'tunnel', '--config', str(tunnel_config), '--no-autoupdate', '--url', local]
            with running_process(command, tunnel_log) as tunnel:
                public = wait_for_address('Cloudflare Quick Tunnel', tunnel, tunnel_log, TUNNEL_ORIGIN, 45)
                write_private(endpoint_file, public + '/revit/')
                print(f'Webhook endpoint: {public}/aps/publish', flush=True)
                yield public + '/aps/publish', {'Webhook relay': relay, 'Cloudflare Quick Tunnel': tunnel}


def publication_status(runtime):
    bridge = json.loads((runtime / '.state/openclaw/integrations/revit/server.json').read_text(encoding='utf-8'))
    request = Request('http://127.0.0.1:9082/publications',
                      headers={'Authorization': 'Bearer ' + bridge['token']})
    with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=10) as response:
        return json.load(response)


def print_status(status):
    print(f"Source: {status['source']} · Model: {status['model']}")
    if status.get('scan_error'):
        print('APS scan: ' + status['scan_error'])
    for row in status['publications'][:2]:
        print(f"Version {row['version']}: {row['status']} · Discord: {row['alert']['status']}")
        if row['error']:
            print(row['error'])
        if row['report']:
            for room in row['report']['discrepancies']:
                if room.get('kind') == 'name':
                    print(f"  {room['number']}: expected {room['expected']}, found {room['actual']}")
                elif room.get('kind') in ('no_area', 'not_in_schedule'):
                    print(f"  {room['number']}: {room['kind'].replace('_', ' ')}")
                else:
                    print(f"  {room['number']}: {room['model_m2']} m², brief {room['brief_m2']} m², difference {room['delta_m2']} m²")
        if row['alert']['url']:
            print(row['alert']['url'])
    if not status['publications']:
        print('Waiting for a matching publish event.')


def watch_publications(runtime, processes):
    detach_sync()
    previous = object()
    while True:
        for label, process in processes.items():
            if (code := process.poll()) is not None:
                raise SetupError(f'{label} stopped (status {code}). Rerun --auto to register a new endpoint.')
        try:
            status = publication_status(runtime)
        except (OSError, ValueError):
            status = None
        if status != previous:
            if status is None:
                print('Waiting for the local publication service.')
            else:
                print_status(status)
            previous = status
        time.sleep(3)


def quote_powershell(value):
    return "'" + value.replace("'", "''") + "'"


def windows_powershell(command):
    encoded = base64.b64encode(command.encode('utf-16-le')).decode('ascii')
    return subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
                          capture_output=True, text=True, errors='replace', timeout=10)


def publish_windows_enrollment(source, destination):
    # The native rename preserves the ACL and refuses to replace an existing file.
    result = windows_powershell(f"""
try {{
    [IO.File]::Move({quote_powershell(source)}, {quote_powershell(destination)})
}} catch {{
    [Console]::Error.WriteLine($_.Exception.GetBaseException().Message)
    exit 1
}}
""")
    if result.returncode:
        detail = result.stderr.strip() or f'PowerShell exited with status {result.returncode}.'
        raise SetupError('Windows could not publish the enrollment file. ' + detail[:240])


def deliver_enrollment(enrollment, checkout, config_name='config.json'):
    if 'microsoft' not in platform.release().lower() and not os.environ.get('WSL_DISTRO_NAME'):
        print(f'Transfer {enrollment} privately to the Revit workstation.', flush=True)
        return

    def windows_path(path):
        return subprocess.check_output(['wslpath', '-w', str(path)], text=True).strip()

    if not re.match(r'^[A-Za-z]:\\', windows_path(checkout)):
        answer = input('Windows Sonne checkout (e.g. C:\\Projects\\Sonne), or Enter for a separate workstation: ').strip()
        if not answer:
            print(f'Transfer {enrollment} privately to the Revit workstation.', flush=True)
            return
        if not re.match(r'^[A-Za-z]:[\\/]', answer):
            raise SetupError('Enter the full Windows drive path to the Sonne checkout.')
        checkout = Path(subprocess.check_output(['wslpath', '-u', answer], text=True).strip())
    if not (checkout / 'scripts/install-revit-client.cmd').is_file():
        raise SetupError('The selected Windows checkout must contain scripts/install-revit-client.cmd.')

    directory = checkout / '.state/revit-client'
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / config_name
    display = windows_path(target)
    location = enrollment.with_suffix('.windows-path')
    updated = None
    if target.exists():
        existing = json.loads(target.read_text(encoding='utf-8-sig'))
        incoming = json.loads(enrollment.read_text())
        if existing.get('token') != incoming['token']:
            raise SetupError('The Windows client is enrolled with another token. Select its matching checkout.')
        if existing.get('url') == incoming['url']:
            write_private(location, windows_path(checkout) + '\n')
            return target
        updated = {**existing, 'url': incoming['url']}

    # Windows permissions on a mounted drive are controlled by NTFS.
    # Restrict an empty file before copying the token, then publish it atomically.
    identity = subprocess.check_output(['whoami.exe', '/user', '/fo', 'csv', '/nh'])
    sid = re.search(rb'S-1-[\d-]+', identity)
    if not sid:
        raise SetupError('Could not identify the Windows account for private enrollment.')
    with NamedTemporaryFile(dir=directory, prefix='.enroll-', delete=False) as handle:
        temp = Path(handle.name)
    try:
        subprocess.run(['icacls.exe', windows_path(temp), '/inheritance:r', '/grant:r',
                        '*' + sid[0].decode('ascii') + ':F', '*S-1-5-18:F'],
                       check=True, stdout=subprocess.DEVNULL)
        if updated is not None:
            temp.write_text(json.dumps(updated, indent=2), encoding='utf-8')
            os.replace(temp, target)
            write_private(location, windows_path(checkout) + '\n')
            print('Revit tunnel URL updated. Restart the Windows client to reconnect.', flush=True)
            return target
        shutil.copyfile(enrollment, temp)
        publish_windows_enrollment(windows_path(temp), display)
    finally:
        temp.unlink(missing_ok=True)
    write_private(location, windows_path(checkout) + '\n')
    print(f'Installed Windows client configuration: {display}', flush=True)
    return target


def check_windows_https(url, certificate):
    windows_certificate = subprocess.check_output(['wslpath', '-w', str(certificate)], text=True, timeout=5).strip()
    probe = """
fetch(process.argv[1], {signal: AbortSignal.timeout(3000), redirect: 'error'})
  .then(async response => {
    if (!response.ok) throw new Error('HTTP ' + response.status);
    if ((await response.json()).ok !== true) throw new Error('Unexpected router health response.');
  }).catch(error => {
    console.error('HTTPS probe:', error.cause?.code || error.message);
    process.exitCode = 1;
  });
"""
    command = f"""
$ErrorActionPreference = 'Stop'
$env:NODE_EXTRA_CA_CERTS = {quote_powershell(windows_certificate)}
$node = (Get-Command node.exe -ErrorAction SilentlyContinue).Source
if (!$node) {{
    $node = Join-Path $env:ProgramFiles 'nodejs/node.exe'
    if (!(Test-Path -LiteralPath $node -PathType Leaf)) {{
        [Console]::Error.WriteLine('Windows Node.js is missing from PATH. Install Node.js 22 or 24.')
        exit 1
    }}
}}
& $node -e {quote_powershell(probe)} {quote_powershell(url)}
exit $LASTEXITCODE
"""
    result = windows_powershell(command)
    if result.returncode:
        output = result.stderr or result.stdout
        detail = next((line.strip() for line in reversed(output.splitlines()) if line.startswith('HTTPS probe:')), '')
        if not detail:
            detail = next((line.strip() for line in reversed(output.splitlines()) if line.strip()), '')
        raise SetupError(detail[:240] or f'Windows HTTPS probe exited with status {result.returncode}.')


def setup_local_revit(runtime):
    if 'microsoft' not in platform.release().lower() and not os.environ.get('WSL_DISTRO_NAME'):
        raise SetupError('Run local Revit setup in WSL on the Windows Revit workstation.')
    source = Path(__file__).resolve().parents[1]
    url = 'https://127.0.0.1:9443/revit/'
    client_id = 'windows-local'
    subprocess.run(['bash', str(source / 'scripts/setup-revit-server.sh'), str(runtime),
                    url, client_id, 'Local Revit workstation'], check=True)
    caddyfile = runtime / 'integrations/revit/Caddyfile.local'
    source_caddyfile = source / 'integrations/revit/Caddyfile.local'
    if source_caddyfile.resolve() != caddyfile.resolve():
        shutil.copyfile(source_caddyfile, caddyfile)
    # The public config must be readable by Caddy with all capabilities dropped.
    caddyfile.chmod(0o644)
    compose = compose_command(runtime, profile='revit-local')
    if os.environ.get('SONNE_DOCKER_USE_SUDO') == '1':
        compose = ['sudo', *compose]
    subprocess.run([*compose, 'up', '-d', '--no-deps', '--force-recreate', 'revit-local-https'],
                   cwd=runtime, check=True)
    with TemporaryDirectory(prefix='sonne-local-tls-') as directory:
        cert = Path(directory) / 'local-ca.crt'
        deadline = time.monotonic() + 30
        detail = 'The local CA certificate is not ready.'
        while time.monotonic() < deadline:
            try:
                result = subprocess.run([*compose, 'exec', '-T', 'revit-local-https', 'cat',
                                         '/data/caddy/pki/authorities/local/root.crt'],
                                        cwd=runtime, capture_output=True, timeout=5)
                if result.returncode == 0:
                    cert.write_bytes(result.stdout)
                    check_windows_https(url + 'health', cert)
                    break
                error = result.stderr.decode('utf-8', errors='replace').strip()
                if error:
                    detail = 'Could not read the local CA from Caddy. ' + error.splitlines()[-1][:240]
            except SetupError as error:
                detail = str(error)
            except subprocess.TimeoutExpired:
                detail = 'Timed out waiting for the local CA or the Windows HTTPS probe.'
            time.sleep(1)
        else:
            raise SetupError(f'Windows could not reach the Revit router at {url}health. {detail}')
        enrollment = runtime / '.state/revit-hub' / f'enroll-{client_id}.json'
        installed = deliver_enrollment(enrollment, source, config_name='config-local.json')
        if installed is None:
            raise SetupError('Choose the Windows checkout on this computer to complete local enrollment.')
        write_private(installed.with_name('local-ca.crt'), cert.read_text(encoding='ascii'))
    print('Local Revit HTTPS is ready at ' + url, flush=True)


def prepare_revit(runtime, local, url):
    if not all((runtime / name).is_file() for name in ('.env', 'compose.yaml', 'compose.prompt-studio.yaml')):
        raise SetupError('Install Sonne and Prompt Studio before configuring ACC.')
    if local.exists():
        config = json.loads(local.read_text(encoding='utf-8'))
        if config['source'] != 'local':
            raise SetupError('The original room publication configuration must use the local source.')
    else:
        config = {
            'source': 'local',
            'model_key': 'snowdon-test',
            'model_label': 'Snowdon Towers test model',
            'schedule_id': 'e12b7ec2-00e3-47be-9f28-c7014662ec50-0032c689',
            'worker': 'windows-01',
            'brief': {
                'revision': 'TEST-01',
                'tolerance_m2': 0.5,
                'rooms': [{'number': 'SNE-1', 'area_m2': 60}, {'number': 'SNE-2', 'area_m2': 30}],
            },
        }
    worker = config['worker']
    if not isinstance(worker, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', worker):
        raise SetupError('The publication worker must be a valid Revit client ID.')
    enrollment = runtime / '.state/revit-hub' / f'enroll-{worker}.json'
    parts = urlsplit(url)
    if (parts.scheme != 'https' or not parts.hostname or parts.username or parts.password or
            parts.query or parts.fragment or not parts.path.endswith('/revit/') or any(c.isspace() for c in url)):
        raise SetupError('Enter the Windows-facing HTTPS URL ending in /revit/.')

    script = Path(__file__).with_name('setup-revit-server.sh')
    subprocess.run(['bash', str(script), str(runtime), url, worker, f'Revit {worker}', '--enable-publications'],
                   check=True)
    if not local.exists():
        write_json(local, config)
        print('Configured the Snowdon test schedule and its 60 m² / 30 m² brief.', flush=True)
    try:
        deliver_enrollment(enrollment, Path(__file__).resolve().parents[1])
    except (OSError, subprocess.CalledProcessError) as error:
        if not enrollment.is_file():
            raise SetupError(f'Revit enrollment file is missing: {enrollment}') from error
        if isinstance(error, OSError):
            detail = f'{error.strerror} ({error.filename or "Windows enrollment"})'
        else:
            detail = f'{error.cmd[0]} exited with status {error.returncode}'
        print(f'Automatic Windows enrollment failed: {detail}.', file=sys.stderr)
        print(f'Transfer {enrollment} privately to the Windows checkout at .state/revit-client/config.json. '
              'For an already enrolled client with the same token, update only its url. '
              'Restart the Windows client after the transfer. Continuing ACC setup.', flush=True)
    print('Ensure the enrolled Revit 2027 client is running with publication reading enabled before publishing.', flush=True)
    return config


@contextmanager
def folder_lock(directory):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (directory / 'worker.lock').open('a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SetupError('ACC folder sync is already running for this installation.') from None
        lock.seek(0)
        lock.truncate()
        lock.write(str(os.getpid()))
        lock.flush()
        yield


def startup_registration(runtime, remove=False):
    directory = runtime / '.state/acc-startup'
    distro = os.environ.get('WSL_DISTRO_NAME')
    user = pwd.getpwuid(os.getuid()).pw_name
    key = hashlib.sha256(os.fsencode((distro or '') + '/' + user + str(runtime))).hexdigest()[:16]
    name = 'Sonne-ACC-' + key
    if distro:
        helper = Path(__file__).with_name('acc-startup-task.ps1')
        windows = subprocess.check_output(['wslpath', '-w', str(helper)], text=True).strip()
        args = ['-Remove'] if remove else ['-Distro', distro, '-LinuxUser', user, '-Runtime', str(runtime)]
        subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'RemoteSigned',
                        '-File', windows, '-Name', name, *args], check=True)
    else:
        unit = Path.home() / '.config/systemd/user' / (name + '.service')
        if remove:
            if unit.exists():
                subprocess.run(['systemctl', '--user', 'disable', '--now', unit.name], check=True)
                unit.unlink()
                subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
            return
        def argument(value):
            return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$').replace('\n', '\\n') + '"'
        command = ' '.join(map(argument, [sys.executable, directory / 'setup-revit-acc.py',
                                          '--runtime', runtime, '--resume-folder']))
        unit.parent.mkdir(parents=True, exist_ok=True)
        write_private(unit, '[Unit]\nDescription=Sonne ACC folder sync and Revit tunnel\n'
                      '[Service]\nUMask=0077\nExecStart=' + command +
                      '\nRestart=on-failure\nRestartSec=30\n[Install]\nWantedBy=default.target\n')
        subprocess.run(['sudo', 'loginctl', 'enable-linger', user], check=True)
        subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
        subprocess.run(['systemctl', '--user', 'enable', unit.name], check=True)


def enable_folder_startup(runtime, project, folder, credentials, tokens, processes, region):
    directory = runtime / '.state/acc-startup'
    with folder_lock(directory):
        for name in ('setup-revit-acc.py', 'forward-revit-aps.py', 'acc-startup-task.ps1'):
            shutil.copyfile(Path(__file__).with_name(name), directory / name)
        saved = dict(project=project, folder=folder, region=region, credentials=list(credentials),
                     refresh_token=tokens['refresh_token'])
        write_json(directory / 'folder.json', saved)
        startup_registration(runtime)
        print('Automatic ACC sync and tunnel startup registered.', flush=True)
        watch_folder(runtime, project, folder, credentials, tokens, processes, region, saved)


def resume_folder(runtime):
    directory = runtime / '.state/acc-startup'
    with folder_lock(directory):
        saved = json.loads((directory / 'folder.json').read_text())
        with quick_tunnel(runtime, revit=True) as (callback, processes):
            url = callback.removesuffix('/aps/publish') + '/revit/'
            for enrollment in (runtime / '.state/revit-hub').glob('enroll-*.json'):
                data = json.loads(enrollment.read_text())
                # Other workers may use their own permanent router. Only rotate
                # enrollments created for a managed Quick Tunnel.
                if not TUNNEL_ORIGIN.fullmatch(data['url'].removesuffix('/revit/')):
                    continue
                data['url'] = url
                write_json(enrollment, data)
                location = enrollment.with_suffix('.windows-path')
                if location.exists() and os.environ.get('WSL_DISTRO_NAME'):
                    try:
                        target = location.read_text().strip()
                        if not re.match(r'^[A-Za-z]:[\\/]', target):
                            raise SetupError('Expected a local Windows drive path.')
                        checkout = subprocess.check_output(['wslpath', '-u', target], text=True).strip()
                        deliver_enrollment(enrollment, Path(checkout))
                    except (OSError, subprocess.CalledProcessError, SetupError) as error:
                        print(f'Could not update the Windows enrollment: {error}. Use the URL in {enrollment}.',
                              file=sys.stderr, flush=True)
            tokens = {'expires_in': 0, 'refresh_token': saved['refresh_token']}
            watch_folder(runtime, saved['project'], saved['folder'], saved['credentials'],
                         tokens, processes, saved['region'], saved)


def setup(args):
    runtime = args.runtime.expanduser().resolve(strict=True)
    if getattr(args, 'local_revit', False):
        setup_local_revit(runtime)
        return
    if getattr(args, 'discovery_dir', None):
        directory = args.discovery_dir.expanduser().resolve(strict=True)
        if not directory.is_dir() or not os.access(directory, os.W_OK):
            raise SetupError('Select a writable shared folder accessible to the Revit workstations.')
        write_json(runtime / '.state/revit-discovery.json', {'directory': str(directory)})
    if getattr(args, 'resume_folder', False):
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        resume_folder(runtime)
        return
    if getattr(args, 'disable_startup', False):
        lock = runtime / '.state/acc-startup/worker.lock'
        stored_pid = lock.read_text() if lock.exists() else ''
        if stored_pid.isdigit():
            pid = int(stored_pid)
            try:
                argv = (Path('/proc') / str(pid) / 'cmdline').read_bytes().split(b'\0')
                if (os.fsencode(runtime / '.state/acc-startup/setup-revit-acc.py') in argv
                        and b'--resume-folder' in argv and os.fsencode(runtime) in argv):
                    os.kill(pid, signal.SIGTERM)
            except (FileNotFoundError, ProcessLookupError):
                pass
        startup_registration(runtime, remove=True)
        return
    if getattr(args, 'remove_hook', False):
        remove_saved_hook(runtime)
        return
    state = runtime / '.state'
    if args.status:
        print_status(publication_status(runtime))
        return
    if args.local:
        deploy(runtime, state / 'revit-publications')
        print('Sonne is serving the local RVT history.')
        return
    local = state / 'revit-publications' / 'config.json'
    if not args.install:
        if not local.is_file() or not (runtime / 'compose.revit.yaml').is_file():
            raise SetupError('Set up the local room publication check first or run the installer with --with-acc.')
        config = json.loads(local.read_text(encoding='utf-8'))
        if config['source'] != 'local':
            raise SetupError('The original room publication configuration must use the local source.')
    if args.auto or args.install:
        with quick_tunnel(runtime, revit=True) if args.install else quick_tunnel(runtime) as (callback, processes):
            if args.install:
                router = callback.removesuffix('/aps/publish') + '/revit/'
                print(f'Revit client URL: {router}', flush=True)
                config = prepare_revit(runtime, local, router)
            folder_watch = configure_acc(runtime, config, callback)
            try:
                if folder_watch:
                    folder_watch(processes)
                else:
                    watch_publications(runtime, processes)
            except KeyboardInterrupt:
                print('\nClosing the webhook relay and tunnel.')
        return
    folder_watch = configure_acc(runtime, config, args.callback)
    if folder_watch:
        folder_watch({})


def configure_acc(runtime, config, callback):
    directory = runtime / '.state/revit-publications-acc'
    selection = input('ACC folder URL (all files), or a direct model URL for a schedule check: ').strip()
    project, folder, lineage = model_ids(selection)
    url = urlsplit(callback)
    if (url.scheme != 'https' or not url.hostname or url.username or url.password or
            url.query or url.fragment or url.path != '/aps/publish'):
        raise SetupError('Use a public HTTPS webhook URL ending in /aps/publish.')
    if LINEAGE.fullmatch(lineage):
        check_callback(callback)
    elif not PROJECT.fullmatch(project) or not FOLDER.fullmatch(folder):
        raise SetupError('Open the folder in ACC Files and copy its URL including folderUrn.')
    region = input('ACC data region (' + '/'.join(REGIONS) + '): ').strip().upper()
    if region not in REGIONS:
        raise SetupError('Use the region where the ACC project is hosted.')
    credentials = (input('APS Client ID: ').strip(), getpass('APS Client Secret: ').strip())
    if not all(credentials):
        raise SetupError('An APS Client ID and Client Secret are required.')
    tokens = authorize(credentials)
    if not LINEAGE.fullmatch(lineage):
        project = 'b.' + PROJECT.fullmatch(project)[1]
        return lambda processes: enable_folder_startup(runtime, project, folder, credentials, tokens, processes, region)
    model = choose_model(selection, tokens['access_token'])
    print(f'ACC model: {model.name}\nPublished baseline: {model.first_version}\nFolder: {model.folder}')
    print(f"Approved Revit schedule UniqueId: {config['schedule_id']}")
    same_hook = directory / 'config.json'
    if same_hook.exists():
        old = json.loads(same_hook.read_text(encoding='utf-8'))
        aps = old['aps']
        if aps['project_id'] != model.project or aps['lineage_urn'] != model.lineage or old['schedule_id'] != config['schedule_id']:
            raise SetupError('The ACC test history is already assigned to a different model or schedule.')
        if aps.get('region') != region:
            raise SetupError('Use the region previously configured for this ACC test model.')
    else:
        aps = {}
    signing_secret = aps.get('webhook_secret') or secrets.token_hex(24)
    hook = aps.get('hook_id')
    new_hook = hook
    if aps.get('callback_url') != callback or not hook:
        management, _ = aps_request('/authentication/v2/token', label='APS webhook setup authorization', credentials=credentials,
                                    form={'grant_type': 'client_credentials', 'scope': 'data:read data:write'})
        management_token = management['access_token']
        hook_body = {
            'callbackUrl': callback, 'projectId': model.project,
            'scope': {'folder': model.folder}, 'token': signing_secret
        }
        body, headers = aps_request(HOOK_PATH, bearer=management_token, region=region,
                                    label='APS publish webhook creation', body=hook_body)
        new_hook = hook_id(body, headers)
    config.update(source='aps', model_key='snowdon-room-acc-test', model_label=model.name)
    config['aps'] = {
        'project_id': model.project, 'lineage_urn': model.lineage, 'first_version': aps.get('first_version', model.first_version),
        'hook_id': new_hook, 'webhook_secret': signing_secret, 'callback_url': callback, 'region': region
    }
    directory.mkdir(mode=0o700, exist_ok=True)
    (directory / 'archives').mkdir(mode=0o700, exist_ok=True)
    write_json(directory / 'aps-auth.json', {
        'client_id': credentials[0], 'client_secret': credentials[1], 'refresh_token': tokens['refresh_token']
    })
    write_json(same_hook, config)
    print(f'APS webhook {new_hook} configured for {model.name}.', flush=True)
    deploy(runtime, directory)
    if hook and hook != new_hook:
        # APS cannot update a hook URL. Retire the previous hook after the replacement is live.
        try:
            request = Request(APS + HOOK_PATH + '/' + quote(hook, safe=''), method='DELETE',
                              headers={'Authorization': 'Bearer ' + management_token, 'region': region})
            with build_opener(NoRedirect()).open(request, timeout=20):
                pass
        except HTTPError as error:
            if error.code != 404:
                print(f'Remove the previous webhook {hook} in APS (HTTP {error.code}).')
        except (URLError, TimeoutError):
            print(f'Remove the previous webhook {hook} in APS if it is still active.')
    print('ACC publication monitoring is ready.')
    print(f"Version {config['aps']['first_version']} is the baseline. The next Publish Latest triggers the comparison.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--discovery-dir', type=Path, help='Shared directory for signed Revit tunnel addresses.')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--local-revit', action='store_true', help='Set up Revit MCP on the same Windows/WSL PC using local HTTPS.')
    mode.add_argument('--upgrade-discovery', action='store_true', help='Update a configured startup worker for internet discovery on its next launch.')
    mode.add_argument('--resume-folder', action='store_true', help='Resume the saved folder sync and Revit tunnel.')
    mode.add_argument('--disable-startup', action='store_true', help='Remove the managed startup registration.')
    mode.add_argument('--remove-hook', action='store_true', help='Unregister the saved Sonne publication webhook.')
    mode.add_argument('--install', action='store_true', help='Provision Revit and the room check in an installed Sonne runtime, then run automatic ACC setup.')
    mode.add_argument('--auto', action='store_true', help='Start a Quick Tunnel, register the hook, and show publication status.')
    mode.add_argument('--callback', help='Register an APS webhook at this public HTTPS URL.')
    mode.add_argument('--local', action='store_true', help='Restore the local RVT history in the running services.')
    mode.add_argument('--status', action='store_true', help='Show capture and alert status for the running service.')
    os.umask(0o077)
    warnings.filterwarnings('error', category=GetPassWarning)
    try:
        args = parser.parse_args()
        if args.install or args.upgrade_discovery:
            if importlib.util.find_spec('cryptography') is None:
                subprocess.run(['sudo', 'apt-get', 'install', '-y', 'python3-cryptography'], check=True)
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        if args.upgrade_discovery:
            directory = args.runtime.expanduser().resolve() / '.state/acc-startup'
            if not (directory / 'folder.json').is_file():
                raise SetupError('Configure folder startup with --install first.')
            for name in ('forward-revit-aps.py', 'setup-revit-acc.py'):
                source = Path(__file__).with_name(name)
                write_private(directory / name, source.read_text(encoding='utf-8'))
            print('Internet discovery installed for the next server startup. Existing enrollments are ready.')
            return 0
        if (args.install or args.auto or args.callback) and not os.environ.get('SONNE_ACC_SESSION'):
            if not shutil.which('tmux') or not shutil.which('pdftotext'):
                subprocess.run(['sudo', 'apt-get', 'install', '-y', 'tmux', 'poppler-utils'], check=True)
            session = 'sonne-' + hashlib.sha256(os.fsencode(args.runtime.resolve())).hexdigest()[:16]
            command = shlex.join(['env', 'SONNE_ACC_SESSION=' + session, sys.executable,
                                  str(Path(__file__).resolve()), *sys.argv[1:]])
            print(f'After setup you can close the terminal. Status: tmux -L sonne-acc attach -t {session}', flush=True)
            return subprocess.call(['tmux', '-L', 'sonne-acc', 'new-session', '-A', '-s', session, command],
                                   env={k: v for k, v in os.environ.items() if k != 'TMUX'})
        setup(args)
    except OSError as error:
        if error.errno == EADDRINUSE:
            print('Port 8765 is already in use. Close the other APS setup terminal and retry.', file=sys.stderr)
        else:
            print(f'ACC setup stopped: {error.strerror} ({error.filename or "local I/O"}).', file=sys.stderr)
        return 1
    except (SetupError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        message = str(error) if isinstance(error, SetupError) else 'Check the runtime path, OAuth callback port and Compose service status.'
        print('ACC setup stopped. ' + message, file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print('\nACC setup cancelled.', file=sys.stderr)
        return 1
    except GetPassWarning:
        print('Open an interactive WSL terminal so the Client Secret can be entered without echo.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
