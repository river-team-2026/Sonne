"""Exercise the guided ACC setup without a public tunnel or Autodesk account."""

from contextlib import contextmanager
import hashlib
import importlib.util
from io import BytesIO
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("setup_revit_acc", ROOT / "scripts/setup-revit-acc.py")
acc = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = acc
spec.loader.exec_module(acc)


class AutomaticAccTests(unittest.TestCase):
    @contextmanager
    def fake_quick_tunnel(self, code):
        processes = []
        tunnel_origins = []
        launch = acc.running_process

        @contextmanager
        def launch_with_fake_cloudflared(command, log_path):
            if command[0] == "fake-cloudflared":
                tunnel_origins.append(command[-1])
                config = Path(command[command.index("--config") + 1])
                self.assertEqual(config.read_text(encoding="utf-8"), "{}\n")
                command = [sys.executable, "-u", "-c", code]
            with launch(command, log_path) as process:
                processes.append(process)
                yield process

        with patch.object(acc.shutil, "which", return_value="fake-cloudflared"):
            with patch.object(acc, "running_process", launch_with_fake_cloudflared):
                yield processes, tunnel_origins

    def test_tunnel_uses_the_running_relay_and_cleans_up_both_processes(self):
        announce = "print('https://test-snowdon.trycloudflare.com', flush=True)"
        with self.fake_quick_tunnel(announce + "\nimport time\ntime.sleep(30)") as (children, origins):
            with acc.quick_tunnel(ROOT, revit=True) as (url, _):
                self.assertEqual(url, "https://test-snowdon.trycloudflare.com/aps/publish")
                self.assertEqual(len(origins), 1)
                opener = build_opener(ProxyHandler({}))
                req = Request(origins[0] + "/aps/publish", data=b"{}")
                with self.assertRaises(HTTPError) as denied:
                    opener.open(req, timeout=3)
                self.assertEqual(denied.exception.code, 401)
                self.assertEqual(denied.exception.headers["X-Sonne-Publish-Relay"], "1")
                denied.exception.close()
                for path, status in (('/revit/register', 401), ('/revit/call', 404)):
                    with self.assertRaises(HTTPError) as blocked:
                        opener.open(Request(origins[0] + path, data=b'{}'), timeout=3)
                    self.assertEqual(blocked.exception.code, status)
                    blocked.exception.close()
                self.assertTrue(all(process.poll() is None for process in children))
            self.assertEqual(len(children), 2)
            self.assertTrue(all(process.poll() is not None for process in children))

    def test_tunnel_start_failure_closes_the_relay(self):
        with self.fake_quick_tunnel("raise SystemExit(7)") as (children, _):
            with self.assertRaisesRegex(acc.SetupError, "Cloudflare Quick Tunnel exited.*status 7"):
                with acc.quick_tunnel(ROOT):
                    self.fail("Expected tunnel startup to fail")
            self.assertEqual(len(children), 2)
            self.assertTrue(all(process.poll() is not None for process in children))

    def test_status_monitor_reports_changes_only(self):
        status = {"source": "aps", "model": "Test", "publications": []}
        updated = dict(status, scan_error="Test error")
        process = Mock()
        process.poll.return_value = None

        with patch.object(acc, "publication_status", side_effect=[status, status, updated]):
            with patch.object(acc.time, "sleep", side_effect=[None, None, KeyboardInterrupt]):
                with patch.object(acc, "print_status") as report:
                    with self.assertRaises(KeyboardInterrupt):
                        acc.watch_publications(ROOT, {"Test tunnel": process})
        self.assertEqual([call.args[0] for call in report.call_args_list], [status, updated])

    def test_automatic_setup_registers_the_discovered_callback_before_monitoring(self):
        config = {"source": "local"}
        callback = "https://test-snowdon.trycloudflare.com/aps/publish"
        processes = {"Tunnel": Mock()}
        stopped = []

        @contextmanager
        def tunnel(runtime):
            yield callback, processes
            stopped.append(runtime)

        with TemporaryDirectory() as temp:
            runtime = Path(temp).resolve()
            state = runtime / ".state/revit-publications"
            state.mkdir(parents=True)
            (state / "config.json").write_text(json.dumps(config), encoding="utf-8")
            (runtime / "compose.revit.yaml").touch()
            args = SimpleNamespace(runtime=runtime, status=False, local=False, auto=True, install=False)
            steps = Mock()
            steps.watch.side_effect = KeyboardInterrupt
            with patch.multiple(acc, quick_tunnel=tunnel, configure_acc=steps.configure, watch_publications=steps.watch):
                acc.setup(args)
            self.assertEqual([call[0] for call in steps.mock_calls], ["configure", "watch"])
            steps.configure.assert_called_once_with(runtime, config, callback)
            steps.watch.assert_called_once_with(runtime, processes)
            self.assertEqual(stopped, [runtime])


class IntegratedAccTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.runtime = Path(temp.name).resolve()
        self.local = self.runtime / '.state/revit-publications/config.json'
        self.local.parent.mkdir(parents=True)
        for name in ('.env', 'compose.yaml', 'compose.prompt-studio.yaml'):
            (self.runtime / name).touch()

    def test_install_provisions_revit_before_starting_acc(self):
        callback = 'https://snowdon.trycloudflare.com/aps/publish'
        args = SimpleNamespace(runtime=self.runtime, status=False, local=False, auto=False, install=True)

        @contextmanager
        def tunnel(runtime, revit=False):
            self.assertEqual(runtime, self.runtime)
            self.assertTrue(revit)
            steps.tunnel()
            yield callback, {}

        steps = Mock()
        steps.watch.side_effect = KeyboardInterrupt
        with patch('builtins.input', return_value='https://sonne.example.com/revit/'):
            with patch.object(acc.subprocess, 'run', steps.provision), patch.object(acc, 'deliver_enrollment') as deliver:
                with patch.multiple(acc, quick_tunnel=tunnel, configure_acc=steps.configure, watch_publications=steps.watch):
                    acc.setup(args)
        deliver.assert_called_once_with(self.runtime / '.state/revit-hub/enroll-windows-01.json', ROOT)
        config = json.loads(self.local.read_text(encoding='utf-8'))
        self.assertEqual([call[0] for call in steps.mock_calls], ['tunnel', 'provision', 'configure', 'watch'])
        provision = steps.provision.call_args.args[0]
        self.assertEqual(Path(provision[1]).name, 'setup-revit-server.sh')
        self.assertEqual(provision[2:], [
            str(self.runtime), 'https://snowdon.trycloudflare.com/revit/', 'windows-01',
            'Revit windows-01', '--enable-publications',
        ])
        steps.configure.assert_called_once_with(self.runtime, config, callback)
        self.assertEqual(config['brief']['rooms'], [
            {'number': 'SNE-1', 'area_m2': 60}, {'number': 'SNE-2', 'area_m2': 30},
        ])

    def test_reuses_the_existing_schedule_and_worker_enrollment(self):
        config = {'source': 'local', 'worker': 'design-pc', 'schedule_id': 'existing-schedule'}
        self.local.write_text(json.dumps(config), encoding='utf-8')
        before = self.local.read_bytes()
        hub = self.runtime / '.state/revit-hub'
        hub.mkdir()
        (hub / 'enroll-design-pc.json').write_text(
            '{"url":"https://existing.example.com/revit/"}', encoding='utf-8',
        )
        with patch('builtins.input', return_value=''), patch.object(acc.subprocess, 'run') as provision, patch.object(acc, 'deliver_enrollment'):
            self.assertEqual(acc.prepare_revit(self.runtime, self.local, 'https://existing.example.com/revit/'), config)
        self.assertEqual(provision.call_args.args[0][3:5], ['https://existing.example.com/revit/', 'design-pc'])
        self.assertEqual(self.local.read_bytes(), before)

    @unittest.skipUnless('microsoft' in acc.platform.release().lower(), 'Needs WSL with Windows interop')
    def test_windows_enrollment_is_private_and_preserves_launch_settings_on_rerun(self):
        temp = acc.subprocess.check_output(['cmd.exe', '/u', '/d', '/c', 'echo %TEMP%']).decode('utf-16-le').strip()
        parent = acc.subprocess.check_output(['wslpath', '-u', temp], text=True).strip()
        enrollment = self.runtime / 'enroll.json'
        enrollment.write_text('{"token":"fixture-token","url":"https://new.example/revit/"}')
        with TemporaryDirectory(dir=parent, prefix='Sonne enrollment ') as folder:
            checkout = Path(folder)
            (checkout / 'scripts').mkdir()
            (checkout / 'scripts/install-revit-client.cmd').touch()
            acc.deliver_enrollment(enrollment, checkout)
            target = checkout / '.state/revit-client/config.json'
            self.assertEqual(target.read_bytes(), enrollment.read_bytes())
            win_target = acc.subprocess.check_output(['wslpath', '-w', str(target)], text=True).strip()
            acl = acc.subprocess.check_output(['icacls.exe', win_target])
            self.assertNotIn(b'(I)', acl)
            target.write_text('{"command":"node","token":"fixture-token","url":"https://old.example/revit/"}')
            acc.deliver_enrollment(enrollment, checkout)
            self.assertEqual(json.loads(target.read_text())['command'], 'node')
            self.assertEqual(json.loads(target.read_text())['url'], 'https://new.example/revit/')
            self.assertEqual(list(target.parent.iterdir()), [target])

    def test_deploy_keeps_the_selected_archive_directory_when_docker_needs_sudo(self):
        directory = self.runtime / '.state/revit-publications-acc'
        with patch.object(acc.shutil, 'copyfile'), patch.dict(acc.os.environ, SONNE_DOCKER_USE_SUDO='1'):
            with patch.object(acc.subprocess, 'run') as compose:
                acc.deploy(self.runtime, directory)
        self.assertEqual(compose.call_args.args[0][:5], [
            'sudo', 'env', 'REVIT_PUBLICATIONS_DIR=' + str(directory), 'docker', 'compose',
        ])
        self.assertIn('REVIT_PUBLICATIONS_DIR=./.state/revit-publications-acc',
                      (self.runtime / '.env').read_text(encoding='utf-8'))


@unittest.skipUnless(sys.platform.startswith("linux"), "Managed cloudflared targets Linux in WSL")
class ManagedCloudflaredTests(unittest.TestCase):
    def test_existing_cloudflared_is_reused(self):
        with patch.object(acc.shutil, "which", return_value="/usr/bin/cloudflared"):
            with patch.object(acc, "urlopen") as download:
                self.assertEqual(acc.resolve_cloudflared(ROOT), "/usr/bin/cloudflared")
        download.assert_not_called()

    def test_verified_binary_is_cached_and_corrupt_binary_is_replaced(self):
        payload = b"cloudflared release binary"
        assets = {"x86_64": ("cloudflared-linux-amd64", hashlib.sha256(payload).hexdigest())}
        with TemporaryDirectory() as temp:
            runtime = Path(temp)
            with patch.object(acc.shutil, "which", return_value=None), patch.object(acc.platform, "machine", return_value="x86_64"):
                with patch.object(acc, "CLOUDFLARED_ASSETS", assets), patch.object(acc, "urlopen", side_effect=[BytesIO(payload), BytesIO(payload)]) as download:
                    target = Path(acc.resolve_cloudflared(runtime))
                    self.assertEqual(target.read_bytes(), payload)
                    self.assertEqual(target.stat().st_mode & 0o777, 0o700)
                    self.assertEqual(acc.resolve_cloudflared(runtime), str(target))
                    self.assertEqual(download.call_count, 1)
                    target.write_bytes(b"corrupt")
                    self.assertEqual(acc.resolve_cloudflared(runtime), str(target))
                    self.assertEqual(target.read_bytes(), payload)
                    self.assertEqual(download.call_count, 2)
        self.assertIn(f"/{acc.CLOUDFLARED_VERSION}/cloudflared-linux-amd64", download.call_args.args[0])

    def test_invalid_download_is_never_installed(self):
        with TemporaryDirectory() as temp:
            runtime = Path(temp)
            with patch.object(acc.shutil, "which", return_value=None), patch.object(acc.platform, "machine", return_value="aarch64"):
                with patch.object(acc, "urlopen", return_value=BytesIO(b"corrupt")) as download:
                    with self.assertRaisesRegex(acc.SetupError, "failed SHA-256 verification"):
                        acc.resolve_cloudflared(runtime)
                directory = runtime / ".state/revit-publications-acc/bin"
                self.assertFalse(list(directory.iterdir()))
        self.assertIn(f"/{acc.CLOUDFLARED_VERSION}/cloudflared-linux-arm64", download.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
