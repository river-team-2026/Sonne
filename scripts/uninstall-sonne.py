#!/usr/bin/env python3
"""Remove an automatic Sonne installation from the Linux server or WSL."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess
import sys
import time


PROJECT = 'sonne'
SCRIPTS = Path(__file__).resolve().parent
MARKER = '.sonne-auto-installer'


def marked_checkout(path):
    marker = path / MARKER
    return (path.is_absolute() and path.resolve() == path and not marker.is_symlink()
            and marker.is_file() and marker.stat().st_size <= 64
            and re.fullmatch(r'[0-9a-f]{40}', marker.read_text().strip())
            and (path / '.git').is_dir() and (path / 'compose.yaml').is_file()
            and (path / 'templates/openclaw.json').is_file())


def discover_runtimes(root=Path('/')):
    # Search for the installer's marker, even after containers have been removed.
    # DrvFs drives are searched by the native Windows helper. Avoid virtual
    # filesystems and Docker layers containing private copies of user files.
    skip = {root / name for name in ('proc', 'sys', 'dev', 'run', 'var/lib/docker', 'var/lib/containerd',
                                      'var/lib/snapd/snap', 'snap', 'mnt/wsl', 'mnt/wslg')}
    if root == Path('/'):
        for line in Path('/proc/self/mountinfo').read_text().splitlines():
            fields, fs = line.split(' - ', 1)
            if fs.split()[0] in ('9p', 'drvfs', 'cifs', 'nfs', 'nfs4', 'fuse.sshfs'):
                skip.add(Path(re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), fields.split()[4])))
    prune = []
    for path in sorted(skip):
        prune.extend(['-path', str(path), '-o'])
    command = ['find', str(root), '(', *prune, '-name', '.git', '-o', '-name', '.cache',
               '-o', '-name', 'node_modules', ')',
               '-prune', '-o', '-type', 'f', '-name', MARKER, '-print0']
    located = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    candidates = {Path(os.fsdecode(p)).parent for p in located.stdout.split(b'\0') if p}
    if root == Path('/'):
        # A mounted custom runtime can also be located from its Docker working directory.
        if shutil.which('docker'):
            try:
                rows = subprocess.run(['docker', 'ps', '-a', '--filter', f'label=com.docker.compose.project={PROJECT}',
                                       '--format', '{{.Label "com.docker.compose.project.working_dir"}}'],
                                      capture_output=True, text=True, timeout=8)
                candidates.update(Path(row) for row in rows.stdout.splitlines() if Path(row).is_absolute())
            except subprocess.TimeoutExpired:
                pass
        for account in pwd.getpwall():
            index = Path(account.pw_dir) / '.local/state/sonne/runtimes'
            try:
                for entry in index.glob('*.path'):
                    if entry.is_symlink() or not entry.is_file() or entry.stat().st_size > 4096:
                        continue
                    candidates.add(Path(entry.read_text().rstrip('\n')))
            except (OSError, UnicodeError):
                continue
    found = []
    for path in sorted(candidates):
        try:
            if not marked_checkout(path):
                continue
            owner = pwd.getpwuid(path.stat().st_uid)
            if path == Path('/') or path in (Path(owner.pw_dir), *Path(owner.pw_dir).parents):
                continue
            found.append({'path': str(path), 'user': owner.pw_name})
        except (OSError, KeyError, UnicodeError):
            continue
    return found


def output(*args):
    return subprocess.check_output(args, text=True).strip()


def validate_runtime(path):
    path = path.expanduser().absolute()
    runtime = path.resolve()
    if path != runtime or runtime == Path('/') or runtime in (Path.home(), *Path.home().parents):
        raise ValueError('Select the real installer-created runtime directory.')
    if SCRIPTS.parent == runtime or SCRIPTS.is_relative_to(runtime):
        raise ValueError('Run the uninstaller from a separate installer checkout.')
    if not runtime.exists():
        return runtime
    if not marked_checkout(runtime):
        raise ValueError(f'{runtime} is missing the automatic installer marker.')
    if runtime.stat().st_uid != os.getuid():
        raise ValueError('Run as the Linux user who owns this installation.')
    return runtime


def docker_inventory(runtime, purge):
    if not shutil.which('docker'):
        print('Docker CLI absent. Only installer files on this host can be checked.')
        return [], {}
    docker = ['docker']
    if subprocess.run([*docker, 'info'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
        docker = ['sudo', 'docker']
    label = 'com.docker.compose.project'
    owned = ['--filter', f'label={label}={PROJECT}']
    containers = output(*docker, 'ps', '-aq', *owned).splitlines()
    for container in containers:
        owner = output(*docker, 'inspect', '--format',
                       '{{index .Config.Labels "com.docker.compose.project.working_dir"}}', container)
        if owner != str(runtime):
            raise ValueError(f'Another {PROJECT} Docker installation owns container {container}.')
    resources = {'containers': containers,
                 'networks': output(*docker, 'network', 'ls', '-q', *owned).splitlines()}
    if purge:
        resources['volumes'] = output(*docker, 'volume', 'ls', '-q', *owned).splitlines()
        tags = output(*docker, 'image', 'ls', *owned, '--format', '{{.Repository}}:{{.Tag}}').splitlines()
        resources['images'] = [t for t in tags if t in {
            f'{PROJECT}-{s}:latest' for s in ('prompt-studio', 'revit-hub', 'revit-publications')}]
    return docker, resources


def remove_docker(docker, resources):
    containers = resources.get('containers', [])
    if containers:
        subprocess.run([*docker, 'stop', *containers], check=True)
        options = ['-v'] if 'volumes' in resources else []
        subprocess.run([*docker, 'rm', *options, *containers], check=True)
    for kind, command in [('networks', 'network'), ('volumes', 'volume'), ('images', 'image')]:
        if resources.get(kind):
            subprocess.run([*docker, command, 'rm', *resources[kind]], check=True)


def matching_windows_client(runtime, checkout):
    config = checkout / '.state/revit-client/config.json'
    if not config.is_file():
        return False
    try:
        client = json.loads(config.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        print(f'Cannot verify Windows enrollment in {checkout}. Skipping that client.', file=sys.stderr)
        return False
    if not isinstance(client, dict):
        return False
    for file in (runtime / '.state/revit-hub').glob('enroll-*.json'):
        enrollment = json.loads(file.read_text())
        if client.get('token') and client['token'] == enrollment.get('token'):
            return True
    print('The checkout has a Revit enrollment for another server. Skipping that client.')
    return False


def windows_clients(runtime, override):
    if override:
        path = override.resolve()
        return [path] if matching_windows_client(runtime, path) else []
    hub = runtime / '.state/revit-hub'
    enrollments = list(hub.glob('enroll-*.json'))
    if not enrollments:
        return []
    candidates = {SCRIPTS.parent}
    scan = False
    for enrollment in enrollments:
        record = enrollment.with_suffix('.windows-path')
        if record.is_file():
            path = Path(output('wslpath', '-u', record.read_text().rstrip('\n')))
            candidates.add(path)
            if matching_windows_client(runtime, path):
                continue
        scan = True
    if scan:
        print('Looking for enrolled Revit clients on local Windows drives...', flush=True)
        helper = output('wslpath', '-w', SCRIPTS / 'find-revit-clients.ps1')
        result = output('powershell.exe', '-NoProfile', '-ExecutionPolicy', 'RemoteSigned',
                        '-File', helper, '-AsJson', '-Scan')
        candidates.update(Path(output('wslpath', '-u', p)) for p in json.loads(result))
    return [path for path in sorted(candidates) if matching_windows_client(runtime, path)]


def erase_runtime(runtime):
    # Keep the marker until all other entries are gone so a failed purge can be retried.
    for child in runtime.iterdir():
        if child.name != MARKER:
            cmd = ['rm', '-rf', '--one-file-system', '--', str(child)]
            if subprocess.run(cmd).returncode:
                subprocess.run(['sudo', *cmd], check=True)
    (runtime / MARKER).unlink()
    runtime.rmdir()


def check_acc_terminal(runtime):
    for process in Path('/proc').glob('[0-9]*'):
        try:
            argv = (process / 'cmdline').read_bytes().decode(errors='replace').rstrip('\0').split('\0')
            if (len(argv) > 2 and Path(argv[1]).name == 'setup-revit-acc.py' and '--runtime' in argv
                    and {'--auto', '--install'}.intersection(argv)):
                target = Path(argv[argv.index('--runtime') + 1])
                if (process / 'cwd' / target).resolve() == runtime:
                    raise ValueError('Press Ctrl+C in the ACC setup terminal to close its tunnel, then retry.')
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue


def run(args):
    if args.dir is None or getattr(args, 'discover_json', False) or getattr(args, 'list', False):
        installs = discover_runtimes()
        if getattr(args, 'discover_json', False):
            print(json.dumps(installs))
            return
        for index, install in enumerate(installs, 1):
            print(f"{index}. {install['path']} ({install['user']})")
        if not installs:
            print('No automatic Sonne installation found.')
            return
        if getattr(args, 'list', False):
            return
        owned = [item for item in installs if item['user'] == pwd.getpwuid(os.getuid()).pw_name]
        if not owned:
            raise ValueError('Run as the listed installation owner.')
        if len(owned) == 1:
            args.dir = Path(owned[0]['path'])
        else:
            number = int(input('Choose the installation number: '))
            if not 1 <= number <= len(installs) or installs[number - 1] not in owned:
                raise ValueError('Choose an installation owned by this user.')
            args.dir = Path(installs[number - 1]['path'])
    runtime = validate_runtime(args.dir)
    if not runtime.exists():
        print(f'No automatic installation at {runtime}.')
        return
    print(f'Sonne runtime: {runtime}', flush=True)
    if not args.keep_data and not args.purge:
        choice = input('1. Uninstall and keep data for reinstall [default]\n'
                       '2. Uninstall and permanently erase Sonne data\nChoose [1/2]: ').strip()
        if choice not in ('', '1', '2'):
            raise ValueError('Enter 1 or 2.')
        args.purge = choice == '2'
    if args.purge:
        print('Purge removes the runtime directory, saved credentials, workspace, model cache, '
              'Sonne-built images and the registered publication webhook.', flush=True)
        if not args.yes and input('Type DELETE to continue: ').strip() != 'DELETE':
            print('Cancelled.')
            return
        # Refuse nested mounts, including bind mounts on the same filesystem.
        for line in Path('/proc/self/mountinfo').read_text().splitlines():
            mount = Path(re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), line.split()[4]))
            if mount.is_relative_to(runtime):
                raise ValueError(f'Unmount {mount} before purging.')
    if shutil.which('tmux'):
        session = 'sonne-' + hashlib.sha256(os.fsencode(runtime)).hexdigest()[:16]
        tmux = ['tmux', '-L', 'sonne-acc']
        def active():
            return subprocess.run([*tmux, 'has-session', '-t', session], capture_output=True).returncode == 0
        if active():
            subprocess.run([*tmux, 'send-keys', '-t', session, 'C-c'], check=True)
            for _ in range(50):
                if not active():
                    break
                time.sleep(0.2)
            else:
                raise ValueError('Stop the ACC background session before uninstalling.')
    if (runtime / '.state/acc-startup/folder.json').is_file():
        subprocess.run([sys.executable, SCRIPTS / 'setup-revit-acc.py', '--runtime', runtime,
                        '--disable-startup'], check=True)
    check_acc_terminal(runtime)
    docker, resources = docker_inventory(runtime, args.purge)
    receipt = runtime / '.sonne-uninstall-started'
    if docker and not resources['containers'] and any(resources.values()) and not receipt.is_file():
        raise ValueError('Docker has Sonne resources without a container linking them to this directory. '
                         'Check which runtime owns them before removing them.')
    if args.purge:
        if shutil.which('wslpath'):
            helper = output('wslpath', '-w', SCRIPTS / 'uninstall-revit-client.ps1')
            for checkout in windows_clients(runtime, args.windows_checkout):
                subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'RemoteSigned',
                                '-File', helper, '-Checkout', output('wslpath', '-w', checkout)], check=True)
        subprocess.run([sys.executable, SCRIPTS / 'setup-revit-acc.py', '--runtime', runtime,
                        '--remove-hook'], check=True)
    if resources.get('containers'):
        receipt.touch(mode=0o600)
    remove_docker(docker, resources)
    if args.purge:
        erase_runtime(runtime)
        key = hashlib.sha256(os.fsencode(runtime)).hexdigest()
        (Path.home() / '.local/state/sonne/runtimes' / (key + '.path')).unlink(missing_ok=True)
        print('Sonne runtime and data removed.')
    else:
        print('Services removed. Runtime files, enrollment and downloaded models are available for reinstall.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--keep-data', action='store_true', help='Remove services and keep data for reinstall.')
    modes.add_argument('--purge', action='store_true', help='Remove services and erase the installer-owned data.')
    parser.add_argument('--yes', action='store_true', help='Confirm the server purge without typing DELETE.')
    parser.add_argument('--dir', type=Path, help='Use a specific runtime instead of discovery.')
    parser.add_argument('--list', action='store_true', help='List detected runtimes and exit.')
    parser.add_argument('--discover-json', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--windows-checkout', type=Path, help='WSL path to a separate local Windows client checkout.')
    try:
        run(parser.parse_args())
    except (OSError, ValueError, subprocess.CalledProcessError, KeyboardInterrupt, EOFError) as error:
        sys.exit(f'Uninstall stopped. {error}')
