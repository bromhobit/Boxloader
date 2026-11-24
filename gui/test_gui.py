#!/usr/bin/env python3
"""Unit and headless PySide6 (offscreen) tests for DopamineLoader GUI."""

from __future__ import annotations

import os
import plistlib
import stat
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

# Ensure headless Qt works cleanly in CI / SSH / no-display environments.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication, QLineEdit

from gui.app import DopamineLoaderWindow
from gui.config import AppPaths, discover_ipa_files, parse_mobileprovision_metadata
from gui.dialogs import AppleLoginDialog, RenewUsbDialog
from gui.protocol import (
    DeviceInfo,
    SecretSanitizer,
    SessionStatus,
    parse_devices_output,
    parse_json_line,
    parse_session_status_output,
    stage_label,
    stage_step_index,
)
from gui.workers import AppleLoginWorker, CoreCommandWorker, DopamineDeployWorker


def _get_qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication(["dopamine-loader-test"])
    return app  # type: ignore[return-value]


def _run_until_signal(signal: object, timeout_ms: int = 5000) -> list[tuple]:
    captured: list[tuple] = []
    loop = QEventLoop()

    def _slot(*args: object) -> None:
        captured.append(args)
        loop.quit()

    signal.connect(_slot)  # type: ignore[attr-defined]
    QTimer.singleShot(timeout_ms, loop.quit)
    loop.exec()
    return captured


class ConfigAndDiscoveryTests(unittest.TestCase):
    def test_default_paths_do_not_reference_codex(self) -> None:
        paths = AppPaths.discover(env={})
        self.assertEqual(paths.outputs_dir, Path.home() / "Downloads")
        self.assertEqual(paths.session_dir, Path.home() / ".local/state/boxloader/apple-session")
        self.assertEqual(paths.core_bin, Path.home() / ".local/libexec/boxloader/dopamine-loader-core")
        for p in (paths.outputs_dir, paths.session_dir, paths.core_bin):
            self.assertNotIn("Codex", str(p))

    def test_paths_discovery_honors_env_and_overrides(self) -> None:
        env = {
            "DOPAMINE_OUTPUTS_DIR": "/tmp/env-out",
            "DOPAMINE_WORK_DIR": "/tmp/env-work",
            "DOPAMINE_LOADER_CORE": "/tmp/env-core",
            "DOPAMINE_SETUP_SCRIPT": "/tmp/env-setup.sh",
            "DOPAMINE_SESSION_DIR": "/tmp/env-sess",
        }
        paths = AppPaths.discover(env=env)
        self.assertEqual(paths.outputs_dir, Path("/tmp/env-out"))
        self.assertEqual(paths.work_dir, Path("/tmp/env-work"))
        self.assertEqual(paths.core_bin, Path("/tmp/env-core"))
        self.assertEqual(paths.setup_script, Path("/tmp/env-setup.sh"))
        self.assertEqual(paths.session_dir, Path("/tmp/env-sess"))

        overridden = AppPaths.discover(env=env, core_override="/tmp/cli-core")
        self.assertEqual(overridden.core_bin, Path("/tmp/cli-core"))

    def test_discover_ipa_files_orders_normal_before_trollstore(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            for name in [
                "DopamineRenew-0.1.0.ipa",
                "SideStore-0.6.1.ipa",
                "DopamineRenew-0.2.0.ipa",
                "SideStore-0.6.3.ipa",
                "SideBox-0.1.0.ipa",
                "Sidebox-1.0.0.ipa",
                "SideStore-0.6.2.ipa",
                "not-an-ipa.deb",
            ]:
                (d / name).write_bytes(b"PK\x03\x04" + b"0" * 100)

            found = discover_ipa_files(d)
            names = [c.name for c in found]
            self.assertEqual(
                names,
                [
                    "Sidebox-1.0.0.ipa",
                    "SideBox-0.1.0.ipa",
                    "SideStore-0.6.3.ipa",
                    "SideStore-0.6.2.ipa",
                    "SideStore-0.6.1.ipa",
                    "DopamineRenew-0.2.0.ipa",
                    "DopamineRenew-0.1.0.ipa",
                ],
            )
            self.assertTrue(found[0].is_featured)
            self.assertTrue(found[1].is_featured)
            self.assertFalse(found[0].is_trollstore_only)
            self.assertFalse(found[2].is_featured)
            self.assertTrue(found[5].is_trollstore_only)
            self.assertTrue(found[5].is_trollstore_only)

    def test_parse_mobileprovision_metadata(self) -> None:
        payload = {
            "AppIDName": "SideStore",
            "TeamIdentifier": ["TEAM123456"],
            "Entitlements": {"application-identifier": "TEAM123456.com.SideStore.SideStore.TEAM123456"},
            "ExpirationDate": datetime(2026, 11, 29, 10, 52, 55, tzinfo=timezone.utc),
            "ProvisionedDevices": ["00000000-0000AABBCCDDEEFF"],
        }
        cms_blob = b"\x30\x82\x01\x00FAKECMS" + plistlib.dumps(payload, fmt=plistlib.FMT_XML) + b"TAIL"
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "test.mobileprovision"
            p.write_bytes(cms_blob)
            meta = parse_mobileprovision_metadata(p)
            self.assertIsNotNone(meta)
            assert meta is not None
            self.assertEqual(meta.app_name, "SideStore")
            self.assertEqual(meta.team_id, "TEAM123456")
            self.assertEqual(meta.bundle_id, "com.SideStore.SideStore.TEAM123456")
            self.assertEqual(meta.device_count, 1)


class ProtocolAndSanitizerTests(unittest.TestCase):
    def test_parse_devices_output_matches_backend_contract(self) -> None:
        raw = (
            '{"ok":true,"usbmuxd_available":true,'
            '"devices":[{"udid":"00000000-0000AABBCCDDEEFF","device_id":1,'
            '"connection_type":"usb","name":"iPhone","product_type":"iPhone12,1",'
            '"product_version":"16.5","paired":true}],'
            '"message":"Найдено устройств: 1","error":null}'
        )
        devices, err = parse_devices_output(raw)
        self.assertIsNone(err)
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0].udid, "00000000-0000AABBCCDDEEFF")
        self.assertTrue(devices[0].paired)
        self.assertIn("iOS 16.5", devices[0].display_label())

        err_raw = (
            '{"ok":false,"usbmuxd_available":false,"devices":[],'
            '"message":"Служба usbmuxd недоступна","error":"connection refused"}'
        )
        devices2, err2 = parse_devices_output(err_raw)
        self.assertEqual(devices2, [])
        self.assertIn("usbmuxd", err2 or "")

    def test_parse_session_status_output(self) -> None:
        ok_raw = (
            '{"ok":true,"exists":true,"valid":true,"email_masked":"a***@icloud.com",'
            '"message":"Сессия Apple ID активна (a***@icloud.com)","error":null}'
        )
        st = parse_session_status_output(ok_raw)
        self.assertIsNotNone(st)
        assert st is not None
        self.assertTrue(st.exists)
        self.assertTrue(st.valid)
        self.assertEqual(st.email_masked, "a***@icloud.com")

        bad_raw = '{"ok":false,"exists":false,"valid":false,"email_masked":null,"message":"Сессия отсутствует"}'
        st2 = parse_session_status_output(bad_raw)
        self.assertIsNotNone(st2)
        assert st2 is not None
        self.assertFalse(st2.valid)

    def test_parse_streaming_events_and_stages(self) -> None:
        ev1 = parse_json_line(
            '{"event":"progress","stage":"signing","percent":45,"message":"Подписываем SideStore"}'
        )
        assert ev1 is not None
        self.assertEqual(ev1.event, "progress")
        self.assertEqual(ev1.stage, "signing")
        self.assertEqual(ev1.percent, 45)
        self.assertEqual(stage_step_index("signing"), 2)
        self.assertEqual(stage_label("signing"), "Подпись приложения")

        ev2 = parse_json_line(
            '{"event":"need_2fa","sms":false,"retry":true,"last_error":"bad code","message":"Введите код"}'
        )
        assert ev2 is not None
        self.assertEqual(ev2.event, "need_2fa")
        self.assertTrue(ev2.payload["retry"])

        ev3 = parse_json_line("plain fallback line")
        assert ev3 is not None
        self.assertEqual(ev3.event, "log")
        self.assertEqual(ev3.message, "plain fallback line")

    def test_secret_sanitizer_redacts_passwords_and_tokens(self) -> None:
        san = SecretSanitizer()
        san.add_transient("SuperSecretPass!99")
        san.add_transient("482910")

        line = (
            'auth failed password=hunter2 X-Apple-GS-Token: gsa-abc123 '
            '{"spd":"secret-spd-val","myacinfo":"cookieval"} '
            'echo SuperSecretPass!99 code=482910'
        )
        cleaned = san.sanitize(line)
        for forbidden in ("hunter2", "gsa-abc123", "secret-spd-val", "cookieval", "SuperSecretPass!99", "482910"):
            self.assertNotIn(forbidden, cleaned)
        self.assertIn("[СКРЫТО]", cleaned)

        san.clear()
        self.assertIn("SuperSecretPass!99", san.sanitize("SuperSecretPass!99"))


class WorkerAndHeadlessGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.qapp = _get_qapp()

    def test_core_command_worker_streams_json_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "mock-core"
            script.write_text(
                "#!/bin/sh\n"
                'echo \'{"event":"progress","stage":"connecting","percent":20,"message":"Подключено"}\'\n'
                'echo \'{"event":"progress","stage":"installing","percent":85,"message":"Установка"}\'\n'
                'echo \'{"event":"done","ok":true,"bundle_id":"com.test.app","message":"Готово"}\'\n',
                encoding="utf-8",
            )
            script.chmod(0o755)

            stages: list[tuple[str, int, str]] = []
            worker = CoreCommandWorker(script, ["install-ipa", "--json"])
            worker.progress.connect(lambda s, p, m: stages.append((s, p, m)))
            worker.start()
            res = _run_until_signal(worker.completed)
            worker.wait(2000)

            self.assertEqual(len(res), 1)
            ok, msg, _raw, payload = res[0]
            self.assertTrue(ok)
            self.assertEqual(msg, "Готово")
            self.assertEqual(payload.get("bundle_id"), "com.test.app")
            self.assertGreaterEqual(len(stages), 2)

    def test_apple_login_worker_stdin_only_and_2fa_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            argv_dump = Path(tmp) / "argv.txt"
            script = Path(tmp) / "mock-login"
            script.write_text(
                "#!/usr/bin/env python3\n"
                "import json, sys\n"
                f"open({str(argv_dump)!r}, 'w').write(' '.join(sys.argv))\n"
                "pwd = sys.stdin.readline().rstrip('\\n')\n"
                "assert pwd == 'MySecretApplePass!7'\n"
                "print(json.dumps({'event':'progress','stage':'authenticating','percent':20,'message':'Проверка учетных данных'}), flush=True)\n"
                "print(json.dumps({'event':'need_2fa','sms':False,'retry':False,'last_error':None,'message':'Введите 6-значный код'}), flush=True)\n"
                "code = sys.stdin.readline().rstrip('\\n')\n"
                "assert code == '654321'\n"
                "print(json.dumps({'event':'done','ok':True,'email_masked':'u***@icloud.com','message':'Сессия сохранена'}), flush=True)\n",
                encoding="utf-8",
            )
            script.chmod(0o755)

            logs: list[str] = []
            worker = AppleLoginWorker(
                script,
                Path(tmp),
                "user@icloud.com",
                "MySecretApplePass!7",
                overwrite=True,
            )
            worker.log_line.connect(logs.append)
            worker.need_2fa.connect(lambda *_args: worker.submit_2fa_code("654321"))
            worker.start()
            res = _run_until_signal(worker.completed)
            worker.wait(2000)

            self.assertEqual(len(res), 1)
            ok, msg, email_masked = res[0]
            self.assertTrue(ok, msg)
            self.assertEqual(email_masked, "u***@icloud.com")

            # Password must NEVER appear in argv or logs.
            recorded_argv = argv_dump.read_text(encoding="utf-8")
            self.assertNotIn("MySecretApplePass!7", recorded_argv)
            joined_logs = "\n".join(logs)
            self.assertNotIn("MySecretApplePass!7", joined_logs)
            self.assertNotIn("654321", joined_logs)

    def test_apple_login_worker_cancel_during_2fa_is_race_free(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "mock-login-cancel"
            script.write_text(
                "#!/usr/bin/env python3\n"
                "import json, sys\n"
                "_ = sys.stdin.readline()\n"
                "print(json.dumps({'event':'need_2fa','sms':False,'retry':False,'last_error':None,'message':'Введите код'}), flush=True)\n"
                "_ = sys.stdin.readline()\n",
                encoding="utf-8",
            )
            script.chmod(0o755)

            worker = AppleLoginWorker(script, Path(tmp), "user@icloud.com", "CancelSecret!1")
            worker.need_2fa.connect(lambda *_args: worker.cancel())
            worker.start()
            res = _run_until_signal(worker.completed)
            worker.wait(2000)

            self.assertEqual(len(res), 1)
            ok, msg, _email = res[0]
            self.assertFalse(ok)
            self.assertIn("отмен", msg.lower())

    def test_dopamine_deploy_worker_redacts_ssh_password(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_mod = Path(tmp) / "fake_deploy.py"
            fake_mod.write_text(
                "from dataclasses import dataclass, field\n"
                "from pathlib import Path\n"
                "@dataclass\n"
                "class DeployRequest:\n"
                "    deb_path: Path | None = None\n"
                "    profile_paths: list[Path] = field(default_factory=list)\n"
                "    session_dir: Path | None = None\n"
                "    ssh_user: str = 'root'\n"
                "    ssh_password: str = ''\n"
                "    udid: str | None = None\n"
                "    setup_script: Path = Path('/bin/true')\n"
                "@dataclass\n"
                "class DeployEvent:\n"
                "    event: str\n"
                "    stage: str = ''\n"
                "    percent: int | None = None\n"
                "    message: str = ''\n"
                "    ok: bool | None = None\n"
                "@dataclass\n"
                "class DeployResult:\n"
                "    ok: bool\n"
                "    message: str\n"
                "    returncode: int = 0\n"
                "    log_lines: list[str] = field(default_factory=list)\n"
                "def deploy_dopamine_renew(req, *, on_event=None, cancel_event=None):\n"
                "    assert req.ssh_password == 'TopSecretSSH!'\n"
                "    if on_event:\n"
                "        on_event(DeployEvent('progress', 'iproxy', 25, 'Туннель готов'))\n"
                "        on_event(DeployEvent('log', 'ssh', None, 'echo TopSecretSSH! leaked'))\n"
                "        on_event(DeployEvent('done', 'done', 100, 'Установлено', True))\n"
                "    return DeployResult(True, 'Установлено', 0, [])\n",
                encoding="utf-8",
            )
            logs: list[str] = []
            worker = DopamineDeployWorker(
                fake_mod,
                deb_path=Path(tmp) / "a.deb",
                profile_paths=[],
                session_dir=None,
                ssh_user="root",
                ssh_password="TopSecretSSH!",
                udid="UDID1",
                setup_script=Path("/bin/true"),
            )
            worker.log_line.connect(logs.append)
            worker.start()
            res = _run_until_signal(worker.completed)
            worker.wait(2000)

            self.assertEqual(len(res), 1)
            ok, msg = res[0]
            self.assertTrue(ok)
            self.assertEqual(msg, "Установлено")
            joined = "\n".join(logs)
            self.assertNotIn("TopSecretSSH!", joined)
            self.assertIn("[СКРЫТО]", joined)

    def test_headless_window_and_dialogs_construction_and_security(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "outputs"
            work_dir = Path(tmp) / "work"
            out_dir.mkdir()
            work_dir.mkdir()
            (out_dir / "SideBox-0.1.0.ipa").write_bytes(b"PK\x03\x04")
            (out_dir / "SideStore-0.6.3.ipa").write_bytes(b"PK\x03\x04")
            (out_dir / "DopamineRenew-0.2.0.ipa").write_bytes(b"PK\x03\x04")
            (out_dir / "DopamineRenew-0.2.0-rootless.deb").write_bytes(b"!<arch>\n")
            sess_dir = work_dir / "dopamine-apple-session"
            sess_dir.mkdir(mode=0o700)
            (sess_dir / "apple-session.plist").write_bytes(b"<plist/>")

            paths = AppPaths.discover(
                outputs_override=out_dir,
                work_override=work_dir,
                session_override=sess_dir,
            )
            win = DopamineLoaderWindow(paths=paths, auto_refresh=False)
            win.show()

            # Renewal/deployment UI is intentionally removed.
            self.assertEqual(win.tabs.count(), 2)
            self.assertIn("IPA", win.tabs.tabText(0))
            self.assertIn("Журнал", win.tabs.tabText(1))
            self.assertFalse(hasattr(win, "deploy_btn"))
            self.assertFalse(hasattr(win, "renew_usb_btn"))
            self.assertFalse(hasattr(win, "pairing_btn"))

            # Default IPA selection chooses featured SideBox-0.1.0.ipa first!
            self.assertTrue(win.ipa_path_edit.text().endswith("SideBox-0.1.0.ipa"))
            self.assertFalse(win.trollstore_notice.isVisible())
            self.assertIn("Sidebox", win.setup_sidebox_btn.text())

            # Verify SideBox setup button constructs exact --target sidebox --sync-session-dir args.
            sidebox_args = win.build_setup_sidebox_args()
            self.assertIn("install-pairing", sidebox_args)
            self.assertIn("--target", sidebox_args)
            self.assertIn("sidebox", sidebox_args)
            self.assertIn("--sync-session-dir", sidebox_args)
            self.assertIn(str(sess_dir), sidebox_args)

            # Selecting DopamineRenew-*.ipa reveals TrollStore notice.
            win._select_ipa_path(out_dir / "DopamineRenew-0.2.0.ipa")
            self.assertTrue(win.trollstore_notice.isVisible())

            # Busy-state management works without removed widgets.
            win._set_operation_busy(True, "ipa")
            self.assertFalse(win.install_ipa_btn.isEnabled())
            win._set_operation_busy(False)
            self.assertTrue(win.install_ipa_btn.isEnabled())

            # Device & Apple session state transitions.
            win.apply_devices(
                [
                    DeviceInfo(
                        udid="00000000-0000AABBCCDDEEFF",
                        device_id=1,
                        name="iPhone",
                        product_type="iPhone12,1",
                        product_version="16.5",
                        paired=True,
                    )
                ]
            )
            self.assertEqual(win.selected_udid(), "00000000-0000AABBCCDDEEFF")
            self.assertEqual(win.paired_pill.objectName(), "PillGreen")

            win.apply_session_status(
                SessionStatus(exists=True, valid=True, email_masked="d***@icloud.com")
            )
            self.assertEqual(win.apple_pill.objectName(), "PillGreen")
            self.assertIn("d***@icloud.com", win.apple_pill.text())

            # Verify stage progression UI.
            win.update_install_stage("uploading", 65, "Отправка на iPhone")
            self.assertEqual(win.ipa_progress_bar.value(), 65)
            self.assertEqual(win._stage_labels["signing"].objectName(), "StageDone")
            self.assertEqual(win._stage_labels["uploading"].objectName(), "StageActive")
            self.assertEqual(win._stage_labels["installing"].objectName(), "StageIdle")

            # Verify log redaction in both drawer & full log tab.
            win.append_log("failed password=supersecret X-Apple-GS-Token=tok123")
            self.assertNotIn("supersecret", win.full_log_edit.toPlainText())
            self.assertNotIn("tok123", win.drawer_log_edit.toPlainText())

            # Verify AppleLoginDialog password mask, clearing on submit, and 2FA reveal.
            login_dlg = AppleLoginDialog(Path("/nonexistent/core"), work_dir, parent=win)
            login_dlg.show()
            self.assertEqual(login_dlg.password_edit.echoMode(), QLineEdit.EchoMode.Password)
            self.assertFalse(login_dlg.tfa_card.isVisible())
            login_dlg.email_edit.setText("invalid-email")
            login_dlg.password_edit.setText("SecretToClear!")
            login_dlg._on_start_login()
            self.assertEqual(login_dlg.password_edit.text(), "")
            login_dlg.show_2fa_prompt("Введите код", retry=True, sms=False, last_error="wrong")
            self.assertTrue(login_dlg.tfa_card.isVisible())
            self.assertIn("wrong", login_dlg.tfa_prompt_label.text())
            login_dlg.close()

            # Verify RenewUsbDialog pre-fill.
            renew_dlg = RenewUsbDialog(
                default_bundle="com.SideStore.SideStore.TEAM123",
                default_team="TEAM123",
                parent=win,
            )
            self.assertEqual(renew_dlg.bundle_id(), "com.SideStore.SideStore.TEAM123")
            self.assertEqual(renew_dlg.team_id(), "TEAM123")
            renew_dlg.close()

            win.close()


if __name__ == "__main__":
    unittest.main()
