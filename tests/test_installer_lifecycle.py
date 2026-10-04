"""Provider installation stays idle; download/delete sessions always release."""
from __future__ import annotations

import tempfile
import os
import shutil
import subprocess
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

from desk import installer, ollama_ctl as ctl, state


class InstallerLifecycle(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(state, 'CONFIG_FILE', root / 'config.json'))
        self.stack.enter_context(patch.object(state, 'LOCK_FILE', root / '.lock'))
        self.stack.enter_context(patch.object(state, 'ensure_dirs', lambda: None))
        self.stack.enter_context(patch.object(installer, 'ensure_dirs', lambda: None))
        self.stack.enter_context(patch.object(ctl, 'binary', return_value='/fake/ollama'))
        self.starts = self.stack.enter_context(patch.object(ctl, 'start'))
        self.stops = self.stack.enter_context(patch.object(ctl, 'stop'))
        self.migration = self.stack.enter_context(patch.object(ctl, 'ensure_background'))
        self.stack.enter_context(patch.object(installer, 'allowed_models', return_value={'a'}))
        self.models = []
        self.stack.enter_context(patch.object(ctl, 'model_inventory', side_effect=lambda: [{'name': n} for n in self.models]))
        self.stack.enter_context(patch.object(ctl, 'list_models', side_effect=lambda: [{'name': n} for n in self.models]))
        self.active = 0
        self.stack.enter_context(patch.object(ctl, 'session', side_effect=self.session))
        installer._cancel.clear()
        self.addCleanup(installer._cancel.clear)

    @contextmanager
    def session(self, *args, **kwargs):
        self.active += 1
        try:
            yield
        finally:
            self.active -= 1

    def test_existing_provider_install_does_not_start_server(self):
        events = list(installer.setup({'stage': 'providers', 'providers': ['ollama']}))
        self.assertTrue(events[-1]['ok'])
        self.starts.assert_not_called()
        self.stops.assert_not_called()
        self.migration.assert_called_once_with()
        self.assertEqual(self.active, 0)

    def test_new_provider_install_does_not_start_server(self):
        binary = self.stack.enter_context(patch.object(ctl, 'binary', return_value=None))
        def install():
            binary.return_value = '/fake/ollama'
            yield {'event': 'log', 'line': 'installed'}
        with patch.object(installer.sys, 'platform', 'darwin'), patch.object(installer, '_install_ollama_app', side_effect=install):
            events = list(installer.setup({'stage': 'providers', 'providers': ['ollama']}))
        self.assertTrue(events[-1]['ok'])
        self.starts.assert_not_called()
        self.stops.assert_not_called()

    def test_success_releases_session_before_done_and_remembers_models(self):
        def pull(name):
            self.assertEqual(self.active, 1)
            self.models.append(name)
            yield 'success'
        with patch.object(ctl, 'pull_model', side_effect=pull):
            for event in installer.setup({'stage': 'models', 'models': ['a']}):
                if event['event'] == 'done':
                    self.assertTrue(event['ok'])
                    self.assertEqual(self.active, 0)
        self.assertEqual(state.load_config()['models'], ['a'])
        self.stops.assert_not_called()

    def test_stream_close_releases_download_session(self):
        stream = installer.setup({'stage': 'models', 'models': ['a']})
        self.assertEqual(next(stream)['event'], 'log')
        self.assertEqual(self.active, 1)
        stream.close()
        self.assertEqual(self.active, 0)

    def test_pull_error_releases_session(self):
        with patch.object(ctl, 'pull_model', side_effect=RuntimeError('download failed')):
            events = list(installer.setup({'stage': 'models', 'models': ['a']}))
        self.assertFalse(events[-1]['ok'])
        self.assertEqual(self.active, 0)

    def test_last_deletion_clears_cache_before_session_release(self):
        self.models[:] = ['a']
        state.save_config({'models': ['a'], 'setup_done': True})
        with patch.object(ctl, 'remove_model', side_effect=lambda n: self.models.remove(n)):
            for event in installer.remove(['a']):
                if event['event'] == 'done':
                    self.assertTrue(event['ok'])
                    self.assertEqual(self.active, 0)
        self.assertEqual(state.load_config()['models'], [])
        self.assertFalse(state.load_config()['setup_done'])
        self.starts.assert_not_called()
        self.stops.assert_not_called()


class LegacyAutostart(unittest.TestCase):
    def test_removes_and_backs_up_only_desks_legacy_login_agent_without_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agents = root / 'Library' / 'LaunchAgents'
            agents.mkdir(parents=True)
            legacy = agents / 'local-llm-desk.ollama.plist'
            legacy.write_text('desk legacy registration')
            other = agents / 'com.ollama.ollama.plist'
            other.write_text('external app registration')
            with patch.object(ctl.sys, 'platform', 'darwin'), patch.object(ctl.Path, 'home', return_value=root), patch.object(ctl, 'PID_DIR', root / 'data' / 'pids'), patch.object(ctl.os, 'getuid', return_value=501), patch.object(ctl.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run, patch.object(ctl, 'start') as start:
                ctl.ensure_background()
            self.assertFalse(legacy.exists())
            self.assertEqual(other.read_text(), 'external app registration')
            self.assertEqual([p.read_text() for p in (root / 'data' / 'backups').iterdir()], ['desk legacy registration'])
            self.assertEqual([c.args[0] for c in run.call_args_list], [
                ['launchctl', 'print', 'gui/501/local-llm-desk.ollama'],
                ['launchctl', 'bootout', 'gui/501/local-llm-desk.ollama'],
            ])
            start.assert_not_called()

    def test_absent_agent_does_not_invoke_launchctl_or_create_backups(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(ctl.sys, 'platform', 'darwin'), patch.object(ctl.Path, 'home', return_value=root), patch.object(ctl.subprocess, 'run') as run, patch.object(ctl, 'start') as start:
                ctl.ensure_background()
            run.assert_not_called()
            start.assert_not_called()
            self.assertEqual(list(root.iterdir()), [])


class Launcher(unittest.TestCase):
    @unittest.skipUnless(shutil.which("zsh"), "macOS launcher requires zsh")
    def test_launcher_only_starts_scheduler_and_never_opens_ollama(self):
        source = Path(__file__).resolve().parents[1] / "start.sh"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "desk").mkdir()
            (root / "desk" / "cron_wrap.sh").write_text("")
            (root / "start.sh").write_text(source.read_text())
            bindir = root / "bin"
            bindir.mkdir()
            marker = root / "calls"
            for name in ("python3", "open", "curl", "ollama"):
                script = bindir / name
                if name == "python3":
                    script.write_text('#!/bin/sh\nif [ "$1" = "-c" ]; then exit 0; fi\nprintf "scheduler\n" >> "$LAUNCH_TEST_MARKER"\n')
                else:
                    script.write_text('#!/bin/sh\nprintf "unexpected\n" >> "$LAUNCH_TEST_MARKER"\n')
                script.chmod(0o755)
            result = subprocess.run([shutil.which("zsh"), str(root / "start.sh")], env={**os.environ, "PATH": str(bindir) + ":/usr/bin:/bin", "LAUNCH_TEST_MARKER": str(marker)}, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(marker.read_text(), "scheduler\n")


if __name__ == '__main__':
    unittest.main()
