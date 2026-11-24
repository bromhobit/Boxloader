#!/usr/bin/env python3
"""Unit and mock-process tests for launcher/dopamine_deploy.py."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

LAUNCHER_DIR = Path(__file__).resolve().parent
if str(LAUNCHER_DIR) not in sys.path:
    sys.path.insert(0, str(LAUNCHER_DIR))

import dopamine_deploy as dd  # noqa: E402


def _write_executable(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)
    return path


def _make_mock_iproxy(directory: Path, *, send_banner: bool = True, fail_immediately: bool = False) -> Path:
    """Create a fake `iproxy` executable accepting `-s 127.0.0.1 [-u UDID] LOCAL:REMOTE`."""
    script = f"""#!{sys.executable}
import os, signal, socket, sys, time

argv_log = os.environ.get("MOCK_IPROXY_ARGV_LOG")
pid_file = os.environ.get("MOCK_IPROXY_PID_FILE")
if argv_log:
    with open(argv_log, "w", encoding="utf-8") as f:
        f.write("\\n".join(sys.argv[1:]))
if pid_file:
    with open(pid_file, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))

if {fail_immediately!r}:
    sys.stderr.write("ERROR: usbmuxd unreachable\\n")
    sys.exit(2)

# Verify exact iproxy 2.1.1 colon syntax: LOCAL:REMOTE
mapping = sys.argv[-1]
if ":" not in mapping:
    sys.stderr.write(f"BAD SYNTAX: {{mapping}}\\n")
    sys.exit(3)
local_port = int(mapping.split(":", 1)[0])

srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("127.0.0.1", local_port))
srv.listen(8)
srv.settimeout(0.2)

stop = False
def _handle(_sig, _frame):
    global stop
    stop = True
signal.signal(signal.SIGTERM, _handle)

try:
    while not stop:
        try:
            conn, _ = srv.accept()
        except socket.timeout:
            continue
        try:
            if {send_banner!r}:
                conn.sendall(b"SSH-2.0-OpenSSH_Mock\\r\\n")
        finally:
            conn.close()
finally:
    srv.close()
"""
    return _write_executable(directory / "mock-iproxy", script)


class DopamineDeployTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dopamine-deploy-test-"))
        self.addCleanup(lambda: dd.shutil.rmtree(self.tmp, ignore_errors=True))

        self.fake_deb = self.tmp / "DopamineRenew-0.2.0-rootless.deb"
        self.fake_deb.write_bytes(b"fake-deb")

        self.fake_profile = self.tmp / "app.mobileprovision"
        self.fake_profile.write_bytes(b"fake-profile")

        self.fake_session_dir = self.tmp / "session"
        self.fake_session_dir.mkdir(mode=0o700)
        (self.fake_session_dir / "apple-session.plist").write_bytes(b"secret-session")
        (self.fake_session_dir / "apple-session.plist").chmod(0o600)

    def _pid_alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False

    def test_repr_hides_password_and_redaction(self) -> None:
        secret = "SuperSecretSshPass!99"
        req = dd.DeployRequest(deb_path=self.fake_deb, ssh_password=secret)
        self.assertNotIn(secret, repr(req))
        self.assertEqual(
            dd.redact_secret(f"debug: {secret} failed", secret),
            "debug: [REDACTED] failed",
        )

    def test_validation_rejects_missing_inputs_and_unsafe_user(self) -> None:
        res = dd.deploy_dopamine_renew(dd.DeployRequest())
        self.assertFalse(res.ok)
        self.assertIn("Не выбрано", res.message)

        res2 = dd.deploy_dopamine_renew(
            dd.DeployRequest(deb_path=self.fake_deb, ssh_user="-oProxyCommand=evil")
        )
        self.assertFalse(res2.ok)
        self.assertIn("Некорректное имя пользователя", res2.message)

    def test_happy_path_askpass_permissions_no_secret_leak_and_cleanup(self) -> None:
        secret = "MyTopSecretPass#2026"
        mock_iproxy = _make_mock_iproxy(self.tmp, send_banner=True)
        audit_json = self.tmp / "setup-audit.json"
        iproxy_argv_log = self.tmp / "iproxy.argv"
        iproxy_pid_file = self.tmp / "iproxy.pid"

        # Fake setup-device.sh that inspects argv, env, askpass file perms, calls askpass,
        # verifies host-key yes/no prompts are rejected, and even tries echoing secret to test redaction!
        fake_setup = _write_executable(
            self.tmp / "setup-device.sh",
            f"""#!/usr/bin/env bash
set -euo pipefail
python3 - "$0" "$@" <<'PY'
import json, os, stat, subprocess, sys
from pathlib import Path

askpass = Path(os.environ["SSH_ASKPASS"])
askpass_dir = askpass.parent
secret_file = askpass_dir / "secret"

# 1. Password must NEVER be in argv or env
argv_text = " ".join(sys.argv)
env_text = "\\n".join(f"{{k}}={{v}}" for k, v in os.environ.items())

# 2. Askpass must reject host-key yes/no prompt
yn_proc = subprocess.run([str(askpass), "Are you sure you want to continue connecting (yes/no)?"], capture_output=True, text=True)

# 3. Askpass must return password on real password prompt
pw_proc = subprocess.run([str(askpass), "root@127.0.0.1's password: "], capture_output=True, text=True, check=True)

# 4. Askpass must enforce max call limit (4 calls total, 5th fails)
extra_rcs = []
for _ in range(4):
    extra_rcs.append(subprocess.run([str(askpass), "password:"], capture_output=True, text=True).returncode)

audit = {{
    "argv": sys.argv[1:],
    "secret_in_argv": {secret!r} in argv_text,
    "secret_in_env": {secret!r} in env_text,
    "askpass_require": os.environ.get("SSH_ASKPASS_REQUIRE"),
    "display": os.environ.get("DISPLAY"),
    "dir_mode": oct(stat.S_IMODE(askpass_dir.stat().st_mode)),
    "script_mode": oct(stat.S_IMODE(askpass.stat().st_mode)),
    "secret_mode": oct(stat.S_IMODE(secret_file.stat().st_mode)),
    "askpass_dir": str(askpass_dir),
    "yn_rc": yn_proc.returncode,
    "yn_out": yn_proc.stdout,
    "pw_matches": pw_proc.stdout.rstrip("\\n") == {secret!r},
    "extra_rcs": extra_rcs,
}}
Path({str(audit_json)!r}).write_text(json.dumps(audit), encoding="utf-8")
PY
echo "Prepared config.plist: team=TEST device=00000000-0000AABBCCDDEEFF bundles=com.example.app renew_before_hours=48"
echo "Connecting to root@127.0.0.1:2222..."
echo "Installing DopamineRenew-0.2.0-rootless.deb on device..."
echo "Uploading config.plist to /var/mobile/Library/DopamineRenew/config.plist (0600)..."
echo "Uploading apple-session.plist to /var/mobile/Library/DopamineRenew/apple-session.plist (0600)..."
echo "Verifying controller status on device..."
echo "Accidental leak probe: {secret}"
echo "Device setup complete."
""",
        )

        old_argv_env = os.environ.get("MOCK_IPROXY_ARGV_LOG")
        old_pid_env = os.environ.get("MOCK_IPROXY_PID_FILE")
        os.environ["MOCK_IPROXY_ARGV_LOG"] = str(iproxy_argv_log)
        os.environ["MOCK_IPROXY_PID_FILE"] = str(iproxy_pid_file)
        try:
            events: list[dd.DeployEvent] = []
            req = dd.DeployRequest(
                deb_path=self.fake_deb,
                profile_paths=[self.fake_profile],
                session_dir=self.fake_session_dir,
                ssh_user="root",
                ssh_password=secret,
                udid="00000000-0000AABBCCDDEEFF",
                setup_script=fake_setup,
                iproxy_bin=str(mock_iproxy),
                connect_timeout=5.0,
            )
            result = dd.deploy_dopamine_renew(req, on_event=events.append)
        finally:
            if old_argv_env is None:
                os.environ.pop("MOCK_IPROXY_ARGV_LOG", None)
            else:
                os.environ["MOCK_IPROXY_ARGV_LOG"] = old_argv_env
            if old_pid_env is None:
                os.environ.pop("MOCK_IPROXY_PID_FILE", None)
            else:
                os.environ["MOCK_IPROXY_PID_FILE"] = old_pid_env

        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.returncode, 0)

        # Verify iproxy syntax & cleanup
        iproxy_args = iproxy_argv_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(iproxy_args[:4], ["-s", "127.0.0.1", "-u", "00000000-0000AABBCCDDEEFF"])
        self.assertTrue(iproxy_args[4].endswith(":22"))
        iproxy_pid = int(iproxy_pid_file.read_text(encoding="utf-8").strip())
        self.assertFalse(self._pid_alive(iproxy_pid), "iproxy process was not terminated!")

        # Verify askpass security audit & cleanup
        audit = json.loads(audit_json.read_text(encoding="utf-8"))
        self.assertFalse(audit["secret_in_argv"])
        self.assertFalse(audit["secret_in_env"])
        self.assertEqual(audit["askpass_require"], "force")
        self.assertEqual(audit["display"], "none:0")
        self.assertEqual(audit["dir_mode"], "0o700")
        self.assertEqual(audit["script_mode"], "0o700")
        self.assertEqual(audit["secret_mode"], "0o600")
        self.assertNotEqual(audit["yn_rc"], 0)
        self.assertEqual(audit["yn_out"], "")
        self.assertTrue(audit["pw_matches"])
        self.assertEqual(audit["extra_rcs"], [0, 0, 0, 1])  # 4th total call ok, 5th rejected!
        self.assertFalse(Path(audit["askpass_dir"]).exists(), "askpass temp dir was not deleted!")

        # Verify secret never leaked into returned logs or emitted events
        all_output = "\n".join(result.log_lines + [e.message for e in events])
        self.assertNotIn(secret, all_output)
        self.assertIn("[REDACTED]", all_output)
        self.assertTrue(any(e.event == "done" and e.ok is True for e in events))

    def test_iproxy_immediate_eof_reports_openssh_error_and_cleans_up(self) -> None:
        mock_iproxy = _make_mock_iproxy(self.tmp, send_banner=False)
        iproxy_pid_file = self.tmp / "iproxy-eof.pid"
        fake_setup = _write_executable(self.tmp / "setup-unused.sh", "#!/bin/sh\nexit 0\n")

        os.environ["MOCK_IPROXY_PID_FILE"] = str(iproxy_pid_file)
        try:
            req = dd.DeployRequest(
                deb_path=self.fake_deb,
                setup_script=fake_setup,
                iproxy_bin=str(mock_iproxy),
                connect_timeout=0.8,
            )
            res = dd.deploy_dopamine_renew(req)
        finally:
            os.environ.pop("MOCK_IPROXY_PID_FILE", None)

        self.assertFalse(res.ok)
        self.assertIn("OpenSSH", res.message)
        iproxy_pid = int(iproxy_pid_file.read_text(encoding="utf-8").strip())
        self.assertFalse(self._pid_alive(iproxy_pid))

    def test_iproxy_crash_surfaces_error(self) -> None:
        mock_iproxy = _make_mock_iproxy(self.tmp, fail_immediately=True)
        fake_setup = _write_executable(self.tmp / "setup-unused.sh", "#!/bin/sh\nexit 0\n")
        res = dd.deploy_dopamine_renew(
            dd.DeployRequest(
                deb_path=self.fake_deb,
                setup_script=fake_setup,
                iproxy_bin=str(mock_iproxy),
                connect_timeout=2.0,
            )
        )
        self.assertFalse(res.ok)
        self.assertIn("iproxy", res.message)

    def test_auth_failure_friendly_error_and_cleanup(self) -> None:
        secret = "WrongPassword999"
        mock_iproxy = _make_mock_iproxy(self.tmp, send_banner=True)
        askpass_record = self.tmp / "askpass-dir.txt"
        fake_setup = _write_executable(
            self.tmp / "setup-fail.sh",
            f"""#!/usr/bin/env bash
printf '%s' "$(dirname "$SSH_ASKPASS")" > {str(askpass_record)!r}
echo "root@127.0.0.1: Permission denied (publickey,password,keyboard-interactive)." >&2
exit 255
""",
        )
        res = dd.deploy_dopamine_renew(
            dd.DeployRequest(
                deb_path=self.fake_deb,
                ssh_password=secret,
                setup_script=fake_setup,
                iproxy_bin=str(mock_iproxy),
                connect_timeout=3.0,
            )
        )
        self.assertFalse(res.ok)
        self.assertIn("Неверный пароль SSH", res.message)
        self.assertNotIn(secret, res.message)
        self.assertFalse(Path(askpass_record.read_text(encoding="utf-8")).exists())

    def test_cancellation_terminates_setup_and_iproxy_and_wipes_askpass(self) -> None:
        mock_iproxy = _make_mock_iproxy(self.tmp, send_banner=True)
        iproxy_pid_file = self.tmp / "iproxy-cancel.pid"
        setup_pid_file = self.tmp / "setup-cancel.pid"
        askpass_record = self.tmp / "askpass-cancel.txt"
        fake_setup = _write_executable(
            self.tmp / "setup-slow.sh",
            f"""#!/usr/bin/env bash
echo $$ > {str(setup_pid_file)!r}
printf '%s' "$(dirname "$SSH_ASKPASS")" > {str(askpass_record)!r}
echo "Connecting to root@127.0.0.1..."
sleep 30
""",
        )
        cancel = threading.Event()

        def trigger_cancel() -> None:
            for _ in range(50):
                if setup_pid_file.exists():
                    cancel.set()
                    return
                time.sleep(0.05)
            cancel.set()

        t = threading.Thread(target=trigger_cancel, daemon=True)
        t.start()
        os.environ["MOCK_IPROXY_PID_FILE"] = str(iproxy_pid_file)
        try:
            res = dd.deploy_dopamine_renew(
                dd.DeployRequest(
                    deb_path=self.fake_deb,
                    ssh_password="CancelSecret",
                    setup_script=fake_setup,
                    iproxy_bin=str(mock_iproxy),
                    connect_timeout=3.0,
                ),
                cancel_event=cancel,
            )
        finally:
            os.environ.pop("MOCK_IPROXY_PID_FILE", None)
            t.join(timeout=2.0)

        self.assertFalse(res.ok)
        self.assertEqual(res.returncode, 130)
        self.assertIn("отменена", res.message.lower())
        self.assertFalse(self._pid_alive(int(iproxy_pid_file.read_text().strip())))
        self.assertFalse(self._pid_alive(int(setup_pid_file.read_text().strip())))
        self.assertFalse(Path(askpass_record.read_text()).exists())

    def test_cli_json_mode_with_password_stdin(self) -> None:
        secret = "CliStdinPass!42"
        mock_iproxy = _make_mock_iproxy(self.tmp, send_banner=True)
        fake_setup = _write_executable(
            self.tmp / "setup-cli.sh",
            f"""#!/usr/bin/env bash
echo "Installing DopamineRenew-0.2.0-rootless.deb on device..."
pw="$("$SSH_ASKPASS" "password:")"
[ "$pw" = {secret!r} ] || exit 9
echo "Device setup complete."
""",
        )
        proc = subprocess.run(
            [
                sys.executable,
                str(LAUNCHER_DIR / "dopamine_deploy.py"),
                "--deb",
                str(self.fake_deb),
                "--setup-script",
                str(fake_setup),
                "--iproxy",
                str(mock_iproxy),
                "--password-stdin",
                "--json",
            ],
            input=f"{secret}\n",
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertNotIn(secret, proc.stdout + proc.stderr)
        lines = [json.loads(ln) for ln in proc.stdout.strip().splitlines()]
        self.assertTrue(any(ev.get("event") == "done" and ev.get("ok") is True for ev in lines))


if __name__ == "__main__":
    unittest.main()
