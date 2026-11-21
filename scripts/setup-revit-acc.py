#!/usr/bin/env python3
"""Set up a folder-scoped APS publish hook for the existing room schedule check."""

import argparse
import base64
from contextlib import contextmanager
from dataclasses import dataclass
from errno import EADDRINUSE
from getpass import GetPassWarning, getpass
import hashlib
import hmac
import json
import os
from pathlib import Path
import platform
import re
import secrets
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
LINEAGE = re.compile(r'urn:adsk\.wipprod:dm\.lineage:[\w-]{1,200}', re.ASCII)
FOLDER = re.compile(r'urn:adsk\.wipprod:fs\.folder:co\.[\w-]{1,200}', re.ASCII)
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
    if url.scheme != 'https' or not (url.hostname or '').endswith('.autodesk.com'):
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
        if not FOLDER.fullmatch(folder):
            folder = unquote(input('Test model folder URN: ').strip())
        if not FOLDER.fullmatch(folder):
            raise SetupError('Open the model folder in ACC Files and use its folderUrn.')
        item = item_in_folder(base, folder, token)
        lineage = item['id']
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


def item_in_folder(base, folder, token):
    name = input('Test RVT filename in this folder (including .rvt): ').strip()
    if not name.lower().endswith('.rvt'):
        raise SetupError('Enter the test model filename as shown in ACC Files.')
    path = base + '/folders/' + quote(folder, safe='') + '/contents'
    url = path
    while url:
        if unquote(urlsplit(urljoin(APS, url)).path) != unquote(path):
            raise SetupError('APS supplied an unexpected folder pagination URL.')
        body, _ = aps_request(url, bearer=token, label='ACC folder lookup')
        for item in body['data']:
            if item['type'] == 'items' and item['attributes']['displayName'] == name:
                return item
        url = (body.get('links', {}).get('next') or {}).get('href')
    raise SetupError('The named RVT was not found in that ACC folder.')


def hook_id(response, headers):
    location = headers.get('Location', '')
    value = response.get('hookId') or (response.get('data') or {}).get('hookId')
    value = value or urlsplit(location.rstrip('/')).path.rsplit('/', 1)[-1]
    if not value or not re.fullmatch(r'[\w-]{1,100}', value, flags=re.ASCII):
        raise SetupError('APS did not return a valid webhook ID.')
    return value


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


def compose_command(runtime):
    files = ['compose.yaml', 'compose.override.yaml', 'compose.prompt-studio.yaml', 'compose.revit.yaml']
    command = ['docker', 'compose', '--env-file', '.env']
    for name in files:
        if (runtime / name).is_file():
            command.extend(('-f', name))
    return command + ['--profile', 'revit-publications']


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
def quick_tunnel(runtime):
    cloudflared = resolve_cloudflared(runtime)
    relay_script = Path(__file__).with_name('forward-revit-aps.py')
    with TemporaryDirectory(prefix='sonne-acc-') as temp:
        relay_log = Path(temp) / 'relay.log'
        tunnel_log = Path(temp) / 'cloudflared.log'
        tunnel_config = Path(temp) / 'cloudflared.yml'
        tunnel_config.write_text('{}\n', encoding='utf-8')
        print('Starting the local relay and Cloudflare Quick Tunnel.', flush=True)
        with running_process([sys.executable, str(relay_script), '--port', '0'], relay_log) as relay:
            local = wait_for_address('Webhook relay', relay, relay_log, RELAY_ORIGIN, 10)
            command = [cloudflared, 'tunnel', '--config', str(tunnel_config), '--no-autoupdate', '--url', local]
            with running_process(command, tunnel_log) as tunnel:
                public = wait_for_address('Cloudflare Quick Tunnel', tunnel, tunnel_log, TUNNEL_ORIGIN, 45)
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
    print('Keep this terminal open and publish the change in Revit. Press Ctrl+C to close the relay and tunnel.')
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


def deliver_enrollment(enrollment, checkout):
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
    target = directory / 'config.json'
    display = windows_path(target)
    if target.exists():
        print(f'Using existing Windows client configuration: {display}', flush=True)
        return

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
        shutil.copyfile(enrollment, temp)
        try:
            os.link(temp, target)
        except FileExistsError:
            print(f'Using existing Windows client configuration: {display}', flush=True)
            return
    finally:
        temp.unlink(missing_ok=True)
    print(f'Installed Windows client configuration: {display}', flush=True)


def prepare_revit(runtime, local):
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
    previous = json.loads(enrollment.read_text(encoding='utf-8'))['url'] if enrollment.is_file() else ''
    default = ' [Enter to reuse the existing URL]' if previous else ''
    url = input(f'Windows-facing Revit router HTTPS URL ending in /revit/{default}: ').strip() or previous
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
    deliver_enrollment(enrollment, Path(__file__).resolve().parents[1])
    print('Ensure the enrolled Revit 2027 client is running with publication reading enabled before publishing.', flush=True)
    return config


def setup(args):
    runtime = args.runtime.expanduser().resolve(strict=True)
    state = runtime / '.state'
    if args.status:
        print_status(publication_status(runtime))
        return
    if args.local:
        deploy(runtime, state / 'revit-publications')
        print('Sonne is serving the local RVT history.')
        return
    local = state / 'revit-publications' / 'config.json'
    if args.install:
        config = prepare_revit(runtime, local)
    else:
        if not local.is_file() or not (runtime / 'compose.revit.yaml').is_file():
            raise SetupError('Set up the local room publication check first or run the installer with --with-acc.')
        config = json.loads(local.read_text(encoding='utf-8'))
        if config['source'] != 'local':
            raise SetupError('The original room publication configuration must use the local source.')
    if args.auto or args.install:
        with quick_tunnel(runtime) as (callback, processes):
            configure_acc(runtime, config, callback)
            try:
                watch_publications(runtime, processes)
            except KeyboardInterrupt:
                print('\nClosing the webhook relay and tunnel.')
        return
    configure_acc(runtime, config, args.callback)


def configure_acc(runtime, config, callback):
    directory = runtime / '.state/revit-publications-acc'
    selection = input('ACC browser URL of the test model or its containing folder: ').strip()
    model_ids(selection)
    url = urlsplit(callback)
    if (url.scheme != 'https' or not url.hostname or url.username or url.password or
            url.query or url.fragment or url.path != '/aps/publish'):
        raise SetupError('Use a public HTTPS webhook URL ending in /aps/publish.')
    check_callback(callback)
    region = input('ACC data region (' + '/'.join(REGIONS) + '): ').strip().upper()
    if region not in REGIONS:
        raise SetupError('Use the region where the ACC project is hosted.')
    credentials = (input('APS Client ID: ').strip(), getpass('APS Client Secret: ').strip())
    if not all(credentials):
        raise SetupError('An APS Client ID and Client Secret are required.')
    tokens = authorize(credentials)
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
    print('ACC mode is running. Keep the tunnel open.')
    print(f"Version {config['aps']['first_version']} is the baseline. The next Publish Latest triggers the comparison.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--install', action='store_true', help='Provision Revit and the room check in an installed Sonne runtime, then run automatic ACC setup.')
    mode.add_argument('--auto', action='store_true', help='Start a Quick Tunnel, register the hook, and show publication status.')
    mode.add_argument('--callback', help='Register an APS webhook at this public HTTPS URL.')
    mode.add_argument('--local', action='store_true', help='Restore the local RVT history in the running services.')
    mode.add_argument('--status', action='store_true', help='Show capture and alert status for the running service.')
    os.umask(0o077)
    warnings.filterwarnings('error', category=GetPassWarning)
    try:
        setup(parser.parse_args())
    except OSError as error:
        if error.errno == EADDRINUSE:
            print('Port 8765 is already in use. Close the other APS setup terminal and retry.', file=sys.stderr)
        else:
            print('ACC setup stopped. Check the runtime path and local service status.', file=sys.stderr)
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
