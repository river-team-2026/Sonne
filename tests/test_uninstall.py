"""Uninstaller safety checks. Opt in to the isolated Docker test with SONNE_TEST_DOCKER_IMAGE."""

import importlib.util
import json
import os
from pathlib import Path
from subprocess import run
from tempfile import TemporaryDirectory
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4


def load(name):
    script = Path(__file__).resolve().parents[1] / 'scripts' / (name + '.py')
    spec = importlib.util.spec_from_file_location(name, script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


uninstall = load('uninstall-sonne')
acc = load('setup-revit-acc')


class UninstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = Path(self.temp.name) / 'custom location' / 'Runtime α'
        self.runtime.mkdir(parents=True)
        (self.runtime / '.git').mkdir()
        (self.runtime / 'compose.yaml').touch()
        (self.runtime / 'templates').mkdir()
        (self.runtime / 'templates/openclaw.json').touch()
        (self.runtime / uninstall.MARKER).write_text('a' * 40)

    def test_discovers_verified_custom_path_and_resolves_the_owner(self):
        unrelated = self.runtime.parent / 'Sonne'
        unrelated.mkdir()
        (unrelated / uninstall.MARKER).write_text('b' * 40)
        (unrelated / 'linked runtime').symlink_to(self.runtime, target_is_directory=True)
        account = SimpleNamespace(pw_name='alternate-user', pw_dir='/home/alternate-user')
        with patch.object(uninstall.pwd, 'getpwuid', return_value=account):
            self.assertEqual(uninstall.discover_runtimes(Path(self.temp.name)),
                             [{'user': 'alternate-user', 'path': str(self.runtime)}])

    def test_refuses_unmarked_and_linked_paths(self):
        with self.assertRaises(ValueError):
            uninstall.validate_runtime(self.runtime.parent)
        link = self.runtime.parent / 'link'
        link.symlink_to(self.runtime, target_is_directory=True)
        with self.assertRaises(ValueError):
            uninstall.validate_runtime(link)

    def test_mismatched_container_stops_before_removal(self):
        with patch.object(uninstall.shutil, 'which', return_value='docker'), patch.object(
                uninstall.subprocess, 'run', return_value=MagicMock(returncode=0)), patch.object(
                uninstall, 'output', side_effect=['container', '/different/runtime']):
            with self.assertRaisesRegex(ValueError, 'Another'):
                uninstall.docker_inventory(self.runtime, True)

    def test_only_matching_client_is_selected(self):
        checkout = self.runtime.parent / 'checkout'
        config = checkout / '.state/revit-client/config.json'
        config.parent.mkdir(parents=True)
        enrollment = self.runtime / '.state/revit-hub/enroll-test.json'
        enrollment.parent.mkdir(parents=True)
        enrollment.write_text('{"token":"fixture"}')
        config.write_text('{"token":"other"}')
        self.assertFalse(uninstall.matching_windows_client(self.runtime, checkout))
        config.write_text(enrollment.read_text())
        self.assertTrue(uninstall.matching_windows_client(self.runtime, checkout))

    @unittest.skipUnless(os.environ.get('WSL_DISTRO_NAME'), 'needs Windows PowerShell via WSL')
    def test_windows_discovers_renamed_client_by_files(self):
        import subprocess
        temp = subprocess.check_output(['cmd.exe', '/u', '/d', '/c', 'echo %TEMP%']).decode('utf-16-le').strip()
        with TemporaryDirectory(dir=uninstall.output('wslpath', '-u', temp)) as folder:
            root = Path(folder)
            client = root / 'project files' / 'studio α'
            for candidate in (client, root / 'Sonne'):
                config = candidate / '.state/revit-client/config.json'
                config.parent.mkdir(parents=True)
                config.write_text('{}')
            (client / 'scripts').mkdir()
            (client / 'scripts/install-revit-client.cmd').touch()
            finder = uninstall.output('wslpath', '-w', uninstall.SCRIPTS / 'find-revit-clients.ps1')
            result = uninstall.output('powershell.exe', '-NoProfile', '-File', finder, '-AsJson',
                                      '-SearchRoot', uninstall.output('wslpath', '-w', root))
            self.assertEqual(json.loads(result), [uninstall.output('wslpath', '-w', client)])

    def test_removes_only_configured_aps_hook(self):
        directory = self.runtime / '.state/revit-publications-acc'
        directory.mkdir(parents=True)
        (directory / 'config.json').write_text(json.dumps({'aps': {'hook_id': 'fixture', 'region': 'EMEA'}}))
        (directory / 'aps-auth.json').write_text('{"client_id":"id","client_secret":"secret"}')
        with patch.object(acc, 'aps_request', return_value=({'access_token': 'token'}, {})), patch.object(
                acc, 'build_opener') as opener:
            acc.remove_saved_hook(self.runtime)
            request = opener.return_value.open.call_args.args[0]
            self.assertEqual(request.get_method(), 'DELETE')
            self.assertEqual(request.full_url, acc.APS + acc.HOOK_PATH + '/fixture')
            self.assertEqual(request.get_header('Region'), 'EMEA')

    @unittest.skipUnless(os.getenv('SONNE_TEST_DOCKER_IMAGE'), 'requires a local disposable Docker test')
    def test_keep_then_purge_with_real_docker(self):
        project = 'sonne-test-' + uuid4().hex
        label = 'com.docker.compose.project=' + project
        volume, network = project + '_models', project + '_default'
        image = os.environ['SONNE_TEST_DOCKER_IMAGE']
        docker = lambda *args: uninstall.output('docker', *args)
        docker('network', 'create', '--label', label, network)
        self.addCleanup(run, ['docker', 'network', 'rm', network], capture_output=True)
        docker('volume', 'create', '--label', label, volume)
        self.addCleanup(run, ['docker', 'volume', 'rm', volume], capture_output=True)
        container = docker('run', '-d', '--label', label, '--label',
                           'com.docker.compose.project.working_dir=' + str(self.runtime), '--network', network,
                           '-v', volume + ':/test', '--user', '0', '--entrypoint', 'sleep', image, '180')
        self.addCleanup(run, ['docker', 'rm', '-f', container], capture_output=True)
        saved = self.runtime / 'workspace.txt'
        saved.write_text('fixture')
        docker('exec', container, 'sh', '-c', 'echo fixture > /test/saved')
        with patch.object(uninstall, 'PROJECT', project):
            args = SimpleNamespace(dir=None, keep_data=True, purge=False, windows_checkout=self.runtime.parent / 'client')
            discovered = uninstall.discover_runtimes(Path(self.temp.name))
            with patch.object(uninstall, 'discover_runtimes', return_value=discovered):
                uninstall.run(args)
            self.assertEqual(docker('ps', '-aq', '--filter', 'label=' + label), '')
            self.assertEqual(docker('network', 'ls', '-q', '--filter', 'label=' + label), '')
            self.assertEqual(docker('run', '--rm', '--network', 'none', '-v', volume + ':/test:ro',
                                    '--entrypoint', 'cat', image, '/test/saved'), 'fixture')
            self.assertEqual(saved.read_text(), 'fixture')
            args.keep_data, args.purge, args.yes = False, True, True
            uninstall.run(args)
            self.assertFalse(self.runtime.exists())
            self.assertEqual(docker('volume', 'ls', '-q', '--filter', 'label=' + label), '')
            uninstall.run(args)


if __name__ == '__main__':
    unittest.main()
