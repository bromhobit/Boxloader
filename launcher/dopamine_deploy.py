#!/usr/bin/env python3
"""One-click USB deployment helper for Dopamine Renew (.deb + config + Apple session).

Starts a temporary localhost `iproxy` USB tunnel, passes the SSH password securely via
OpenSSH `SSH_ASKPASS` (private 0700/0600 files, zeroed & deleted in `finally`, never in
CLI argv, env vars, or logs), invokes `package/setup-device.sh`, streams structured
events, and always tears down `iproxy` and temp secrets cleanly.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import selectors
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

LAUNCHER_DIR = Path(__file__).resolve().parent
PROJECT_DIR = LAUNCHER_DIR.parent
OUTPUTS_DIR = Path(os.environ.get("DOPAMINE_OUTPUTS_DIR", str(PROJECT_DIR.parent)))
WORK_DIR = Path(os.environ.get("DOPAMINE_WORK_DIR", str(OUTPUTS_DIR.parent / "work")))

DEFAULT_DEB_PATH = OUTPUTS_DIR / "DopamineRenew-0.2.0-rootless.deb"
DEFAULT_PROFILE_PATH = WORK_DIR / "dopamine-apple-session" / "sidestore-fresh.mobileprovision"
DEFAULT_SESSION_DIR = WORK_DIR / "dopamine-apple-session"
DEFAULT_SETUP_SCRIPT = Path(
    os.environ.get(
        "DOPAMINE_SETUP_SCRIPT",
        str(OUTPUTS_DIR / "dopamine-renew" / "package" / "setup-device.sh"),
    )
)
DEFAULT_CORE_BIN = Path(
    os.environ.get(
        "DOPAMINE_LOADER_CORE",
        str(WORK_DIR / "dopamine-loader-target" / "release" / "dopamine-loader-core"),
    )
)
DEFAULT_ICON_PATH = Path(
    os.environ.get("DOPAMINE_ICON_PATH", str(LAUNCHER_DIR / "icon.png"))
)

_SAFE_USER_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,63}$")
_SAFE_UDID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{7,127}$")
_MAX_ASKPASS_CALLS = 4


@dataclass
class DeployRequest:
    """Parameters for one-click Dopamine Renew USB deployment."""

    deb_path: Path | None = None
    profile_paths: list[Path] = field(default_factory=list)
    session_dir: Path | None = None
    ssh_user: str = "root"
    ssh_password: str = field(default="", repr=False)
    udid: str | None = None
    device_port: int = 22
    renew_before_hours: int = 48
    setup_script: Path = DEFAULT_SETUP_SCRIPT
    iproxy_bin: str = "iproxy"
    connect_timeout: float = 10.0
    ssh_identity: Path | None = None
    probe_ssh_banner: bool = True


@dataclass
class DeployEvent:
    """Structured progress/log/completion event emitted during deployment."""

    event: str  # "progress" | "log" | "done" | "error"
    stage: str = ""
    percent: int | None = None
    message: str = ""
    ok: bool | None = None

    def to_json(self) -> str:
        payload = {k: v for k, v in asdict(self).items() if v is not None}
        return json.dumps(payload, ensure_ascii=False)


@dataclass
class DeployResult:
    """Final result returned by `deploy_dopamine_renew`."""

    ok: bool
    message: str
    returncode: int = 0
    log_lines: list[str] = field(default_factory=list)


def redact_secret(text: str, secret: str) -> str:
    """Ensure the SSH password never leaks into emitted log lines."""
    if not text or not secret:
        return text
    return text.replace(secret, "[REDACTED]")


def pick_unused_local_port() -> int:
    """Bind an ephemeral IPv4 localhost port and return its port number."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _secure_wipe_file(path: Path) -> None:
    """Overwrite a private file with zeros, fsync, truncate, and unlink."""
    try:
        if not path.exists() or path.is_symlink():
            path.unlink(missing_ok=True)
            return
        size = max(path.stat().st_size, 64)
        with open(path, "r+b", buffering=0) as handle:
            handle.seek(0)
            handle.write(b"\x00" * size)
            handle.flush()
            os.fsync(handle.fileno())
            handle.truncate(0)
            os.fsync(handle.fileno())
    except OSError:
        pass
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _create_askpass_bundle(password: str) -> tuple[Path, Path, Path]:
    """Create a 0700 dir with a 0600 password file and 0700 askpass helper script."""
    askpass_dir = Path(tempfile.mkdtemp(prefix="dopamine-askpass-"))
    os.chmod(askpass_dir, 0o700)

    secret_file = askpass_dir / "secret"
    counter_file = askpass_dir / "calls"
    askpass_script = askpass_dir / "askpass.sh"

    fd = os.open(secret_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(password.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        _secure_wipe_file(secret_file)
        shutil.rmtree(askpass_dir, ignore_errors=True)
        raise

    cfd = os.open(counter_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(cfd, "wb") as handle:
        handle.write(b"0\n")

    script_body = (
        "#!/bin/sh\n"
        "set -eu\n"
        f"SECRET_FILE={shlex.quote(str(secret_file))}\n"
        f"COUNTER_FILE={shlex.quote(str(counter_file))}\n"
        'PROMPT="${1:-}"\n'
        'case "$PROMPT" in\n'
        "    *yes/no*|*fingerprint*|*continue\\ connecting*)\n"
        "        exit 1\n"
        "        ;;\n"
        "esac\n"
        'COUNT=0\n'
        'if [ -f "$COUNTER_FILE" ]; then\n'
        '    COUNT="$(cat "$COUNTER_FILE" 2>/dev/null || echo 0)"\n'
        "fi\n"
        f'if [ "$COUNT" -ge {_MAX_ASKPASS_CALLS} ]; then\n'
        "    exit 1\n"
        "fi\n"
        'printf "%s\\n" "$((COUNT + 1))" > "$COUNTER_FILE"\n'
        'if [ ! -f "$SECRET_FILE" ] || [ -L "$SECRET_FILE" ]; then\n'
        "    exit 1\n"
        "fi\n"
        'cat "$SECRET_FILE"\n'
        "printf '\\n'\n"
    ).encode("utf-8")

    sfd = os.open(askpass_script, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
    with os.fdopen(sfd, "wb") as handle:
        handle.write(script_body)
        handle.flush()
        os.fsync(handle.fileno())

    return askpass_dir, secret_file, askpass_script


def _cleanup_askpass_bundle(askpass_dir: Path | None, secret_file: Path | None) -> None:
    if secret_file is not None:
        _secure_wipe_file(secret_file)
    if askpass_dir is not None:
        shutil.rmtree(askpass_dir, ignore_errors=True)


def _terminate_process_group(proc: subprocess.Popen | None, grace_seconds: float = 2.0) -> None:
    """Terminate a process (and its detached process group) cleanly without leaving orphans."""
    if proc is None:
        return
    try:
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except PermissionError:
                proc.terminate()
            deadline = time.monotonic() + grace_seconds
            while proc.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    proc.kill()
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    pass
    finally:
        for pipe in (proc.stdout, proc.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except OSError:
                    pass


def _resolve_executable(binary: str) -> str | None:
    p = Path(binary)
    if p.parent != Path("."):
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
        return None
    return shutil.which(binary)


def _validate_request(request: DeployRequest) -> tuple[str, Path]:
    if not request.deb_path and not request.profile_paths and not request.session_dir:
        raise ValueError("Не выбрано ни одного файла для установки (.deb, профиль или сессия Apple).")

    if not _SAFE_USER_RE.fullmatch(request.ssh_user):
        raise ValueError(f"Некорректное имя пользователя SSH: {request.ssh_user!r}")

    if request.udid is not None and request.udid != "":
        if not _SAFE_UDID_RE.fullmatch(request.udid):
            raise ValueError(f"Некорректный UDID устройства: {request.udid!r}")

    if not (1 <= int(request.device_port) <= 65535):
        raise ValueError(f"Некорректный порт устройства: {request.device_port}")

    if not (1 <= int(request.renew_before_hours) <= 720):
        raise ValueError("Окно продления должно быть от 1 до 720 часов.")

    if request.connect_timeout <= 0:
        raise ValueError("Таймаут подключения должен быть положительным.")

    setup_script = Path(request.setup_script)
    if not setup_script.is_file() or setup_script.is_symlink():
        raise ValueError(f"Скрипт установки не найден: {setup_script}")

    iproxy_resolved = _resolve_executable(request.iproxy_bin)
    if not iproxy_resolved:
        raise ValueError(
            f"Утилита iproxy не найдена ({request.iproxy_bin}). Установите libusbmuxd / usbmuxd."
        )

    if request.deb_path is not None:
        deb = Path(request.deb_path)
        if not deb.is_file() or deb.is_symlink():
            raise ValueError(f"Файл пакета .deb не найден: {deb}")

    for prof in request.profile_paths:
        p = Path(prof)
        if not p.is_file() or p.is_symlink():
            raise ValueError(f"Файл профиля .mobileprovision не найден: {p}")

    if request.session_dir is not None:
        sdir = Path(request.session_dir)
        if not sdir.is_dir() or sdir.is_symlink():
            raise ValueError(f"Каталог сессии Apple не найден: {sdir}")
        splist = sdir / "apple-session.plist"
        if not splist.is_file() or splist.is_symlink():
            raise ValueError(f"В каталоге сессии отсутствует apple-session.plist: {sdir}")

    if request.ssh_identity is not None:
        ident = Path(request.ssh_identity)
        if not ident.is_file():
            raise ValueError(f"SSH-ключ не найден: {ident}")

    return iproxy_resolved, setup_script


def _wait_for_tunnel(
    local_port: int,
    iproxy_proc: subprocess.Popen,
    timeout: float,
    *,
    probe_ssh_banner: bool,
    cancel_event: threading.Event | None,
) -> tuple[bool, str]:
    """Wait for local iproxy TCP listener (and optional SSH banner over USB)."""
    deadline = time.monotonic() + timeout
    saw_tcp_accept = False
    saw_immediate_eof = False

    while time.monotonic() < deadline:
        if cancel_event is not None and cancel_event.is_set():
            return False, "Операция отменена пользователем."

        rc = iproxy_proc.poll()
        if rc is not None:
            err_tail = ""
            if iproxy_proc.stdout is not None:
                try:
                    err_tail = iproxy_proc.stdout.read().decode("utf-8", errors="replace").strip()
                except OSError:
                    err_tail = ""
            detail = f": {err_tail}" if err_tail else ""
            return False, f"Туннель iproxy завершился досрочно (код {rc}){detail}"

        remaining = max(0.2, min(2.0, deadline - time.monotonic()))
        try:
            with socket.create_connection(("127.0.0.1", local_port), timeout=remaining) as sock:
                saw_tcp_accept = True
                if not probe_ssh_banner:
                    return True, ""
                sock.settimeout(min(2.0, remaining))
                banner = sock.recv(16)
                if banner.startswith(b"SSH-"):
                    return True, ""
                if banner == b"":
                    saw_immediate_eof = True
        except OSError:
            pass

        time.sleep(0.15)

    if saw_immediate_eof or saw_tcp_accept:
        return (
            False,
            "Не удалось подключиться к OpenSSH на iPhone (порт 22). "
            "Проверьте USB-кабель, доверие этому ПК и что OpenSSH включён в Dopamine.",
        )
    return (
        False,
        f"Превышено время ожидания локального USB-туннеля iproxy (порт {local_port}).",
    )


def _classify_stage(line: str) -> tuple[str, int, str] | None:
    if line.startswith("Prepared config.plist"):
        return ("connecting", 18, "Подготовлен config.plist из профиля…")
    if line.startswith("Connecting to "):
        return ("connecting", 28, "Подключение к iPhone по USB SSH…")
    if line.startswith("Installing "):
        return ("installing", 48, "Установка .deb пакета (dpkg + uicache + LaunchDaemon)…")
    if "Uploading config.plist" in line:
        return ("uploading", 68, "Копирование config.plist на iPhone (0600)…")
    if "Uploading apple-session.plist" in line:
        return ("uploading", 78, "Копирование сессии Apple ID на iPhone (0600)…")
    if "Uploading anisette_state" in line:
        return ("uploading", 85, "Копирование anisette_state на iPhone (0600)…")
    if line.startswith("Verifying controller status"):
        return ("verifying", 92, "Проверка dopamine-renew-controller на iPhone…")
    return None


def _friendly_error(returncode: int, log_lines: list[str]) -> str:
    joined = "\n".join(log_lines)
    lower = joined.lower()
    if "permission denied" in lower or "authentication failed" in lower:
        return (
            "Неверный пароль SSH или отказ в авторизации на iPhone. "
            "Проверьте пароль пользователя root/mobile (по умолчанию в Dopamine: alpine или ваш пароль)."
        )
    if "connection refused" in lower or "connection closed by remote host" in lower:
        return (
            "OpenSSH на iPhone отклонил соединение. Убедитесь, что OpenSSH активен в Dopamine."
        )
    if "operation timed out" in lower or "connection timed out" in lower:
        return "Превышено время ожидания SSH-соединения с iPhone."
    if "host key verification failed" in lower:
        return "Ошибка проверки ключа хоста SSH (known_hosts)."
    last_non_empty = next((ln for ln in reversed(log_lines) if ln.strip()), "")
    if last_non_empty:
        return f"Установка завершилась с ошибкой (код {returncode}): {last_non_empty}"
    return f"Установка завершилась с ошибкой (код {returncode})."


def deploy_dopamine_renew(
    request: DeployRequest,
    *,
    on_event: Callable[[DeployEvent], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> DeployResult:
    """Run one-click Dopamine Renew deployment over a temporary USB `iproxy` tunnel."""
    log_lines: list[str] = []

    def emit(ev: DeployEvent) -> None:
        if on_event is not None:
            on_event(ev)

    try:
        iproxy_resolved, setup_script = _validate_request(request)
    except ValueError as exc:
        msg = str(exc)
        emit(DeployEvent(event="error", stage="tunnel", percent=0, message=msg, ok=False))
        return DeployResult(ok=False, message=msg, returncode=2, log_lines=log_lines)

    if cancel_event is not None and cancel_event.is_set():
        msg = "Операция отменена пользователем."
        emit(DeployEvent(event="error", stage="cleanup", message=msg, ok=False))
        return DeployResult(ok=False, message=msg, returncode=130, log_lines=log_lines)

    iproxy_proc: subprocess.Popen | None = None
    setup_proc: subprocess.Popen | None = None
    askpass_dir: Path | None = None
    secret_file: Path | None = None

    try:
        local_port = pick_unused_local_port()
        iproxy_cmd = [iproxy_resolved, "-s", "127.0.0.1"]
        if request.udid:
            iproxy_cmd.extend(["-u", request.udid])
        iproxy_cmd.append(f"{local_port}:{int(request.device_port)}")

        emit(
            DeployEvent(
                event="progress",
                stage="tunnel",
                percent=5,
                message=f"Запуск USB-туннеля iproxy (127.0.0.1:{local_port} → iPhone:{request.device_port})…",
            )
        )
        iproxy_proc = subprocess.Popen(
            iproxy_cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

        ready, err_msg = _wait_for_tunnel(
            local_port,
            iproxy_proc,
            request.connect_timeout,
            probe_ssh_banner=request.probe_ssh_banner,
            cancel_event=cancel_event,
        )
        if not ready:
            emit(DeployEvent(event="error", stage="tunnel", message=err_msg, ok=False))
            return DeployResult(ok=False, message=err_msg, returncode=1, log_lines=log_lines)

        emit(
            DeployEvent(
                event="progress",
                stage="connecting",
                percent=15,
                message="USB-туннель открыт, подготовка параметров установки…",
            )
        )

        setup_cmd = [
            "bash",
            str(setup_script),
            "--host",
            "127.0.0.1",
            "--port",
            str(local_port),
            "--user",
            request.ssh_user,
            "--renew-before-hours",
            str(int(request.renew_before_hours)),
        ]
        if request.deb_path is not None:
            setup_cmd.extend(["--deb", str(request.deb_path)])
        for prof in request.profile_paths:
            setup_cmd.extend(["--profile", str(prof)])
        if request.session_dir is not None:
            setup_cmd.extend(["--session-dir", str(request.session_dir)])
        if request.udid:
            setup_cmd.extend(["--device", request.udid])
        if request.ssh_identity is not None:
            setup_cmd.extend(["--identity", str(request.ssh_identity)])

        # Build child environment; NEVER put ssh_password in env or argv.
        child_env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("SSH_ASKPASS", "SSH_ASKPASS_REQUIRE")
        }
        if request.ssh_password:
            askpass_dir, secret_file, askpass_script = _create_askpass_bundle(request.ssh_password)
            child_env["SSH_ASKPASS"] = str(askpass_script)
            child_env["SSH_ASKPASS_REQUIRE"] = "force"
            child_env["DISPLAY"] = "none:0"
        else:
            child_env["DISPLAY"] = ""

        setup_proc = subprocess.Popen(
            setup_cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=child_env,
            start_new_session=True,
        )

        assert setup_proc.stdout is not None
        selector = selectors.DefaultSelector()
        selector.register(setup_proc.stdout, selectors.EVENT_READ)
        buf = b""
        cancelled = False

        try:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    cancelled = True
                    _terminate_process_group(setup_proc)
                    break

                events = selector.select(timeout=0.1)
                if events:
                    chunk = os.read(setup_proc.stdout.fileno(), 4096)
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        raw_line, buf = buf.split(b"\n", 1)
                        line = redact_secret(
                            raw_line.decode("utf-8", errors="replace").rstrip("\r"),
                            request.ssh_password,
                        )
                        if line:
                            log_lines.append(line)
                            emit(DeployEvent(event="log", stage="running", message=line))
                            stage_info = _classify_stage(line)
                            if stage_info is not None:
                                st, pct, msg = stage_info
                                emit(DeployEvent(event="progress", stage=st, percent=pct, message=msg))
                elif setup_proc.poll() is not None:
                    # Drain any immediately available buffered bytes after exit without blocking.
                    while selector.select(timeout=0.05):
                        chunk = os.read(setup_proc.stdout.fileno(), 4096)
                        if not chunk:
                            break
                        buf += chunk
                    break
        finally:
            selector.close()

        if buf and not cancelled:
            for raw_tail in buf.decode("utf-8", errors="replace").splitlines():
                tail = redact_secret(raw_tail.rstrip("\r"), request.ssh_password)
                if tail:
                    log_lines.append(tail)
                    emit(DeployEvent(event="log", stage="running", message=tail))
                    stage_info = _classify_stage(tail)
                    if stage_info is not None:
                        st, pct, msg = stage_info
                        emit(DeployEvent(event="progress", stage=st, percent=pct, message=msg))

        if cancelled:
            msg = "Операция отменена пользователем."
            emit(DeployEvent(event="error", stage="cleanup", message=msg, ok=False))
            return DeployResult(ok=False, message=msg, returncode=130, log_lines=log_lines)

        rc = setup_proc.wait()
        if rc != 0:
            msg = _friendly_error(rc, log_lines)
            emit(DeployEvent(event="error", stage="error", message=msg, ok=False))
            return DeployResult(ok=False, message=msg, returncode=rc, log_lines=log_lines)

        ok_msg = "Dopamine Renew успешно установлен и настроен на iPhone!"
        emit(DeployEvent(event="progress", stage="done", percent=100, message=ok_msg))
        emit(DeployEvent(event="done", stage="done", percent=100, message=ok_msg, ok=True))
        return DeployResult(ok=True, message=ok_msg, returncode=0, log_lines=log_lines)

    except Exception as exc:
        msg = redact_secret(f"Внутренняя ошибка установки: {exc}", request.ssh_password)
        emit(DeployEvent(event="error", stage="error", message=msg, ok=False))
        return DeployResult(ok=False, message=msg, returncode=1, log_lines=log_lines)

    finally:
        _terminate_process_group(setup_proc)
        _terminate_process_group(iproxy_proc)
        _cleanup_askpass_bundle(askpass_dir, secret_file)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="One-click USB deployment for Dopamine Renew via temporary iproxy + setup-device.sh"
    )
    parser.add_argument("--deb", type=Path, help="Path to rootless .deb package")
    parser.add_argument(
        "--profile",
        type=Path,
        action="append",
        default=[],
        help="Path to .mobileprovision profile (repeatable)",
    )
    parser.add_argument("--session-dir", type=Path, help="Directory with apple-session.plist")
    parser.add_argument("--user", default="root", help="SSH user (default: root)")
    parser.add_argument("--udid", help="Optional target iPhone UDID")
    parser.add_argument("--device-port", type=int, default=22, help="Device SSH port (default: 22)")
    parser.add_argument(
        "--renew-before-hours", type=int, default=48, help="Renewal threshold in hours (default: 48)"
    )
    parser.add_argument("--setup-script", type=Path, default=DEFAULT_SETUP_SCRIPT)
    parser.add_argument("--iproxy", default="iproxy", help="Path to iproxy executable")
    parser.add_argument("--identity", type=Path, help="Optional SSH identity file")
    parser.add_argument("--timeout", type=float, default=10.0, help="Tunnel connect timeout in seconds")
    parser.add_argument(
        "--password-stdin",
        action="store_true",
        help="Read SSH password from a single line on stdin (never pass password on CLI)",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON-lines progress events on stdout")
    args = parser.parse_args()

    password = ""
    if args.password_stdin:
        raw = sys.stdin.readline()
        password = raw.rstrip("\r\n")

    req = DeployRequest(
        deb_path=args.deb,
        profile_paths=list(args.profile),
        session_dir=args.session_dir,
        ssh_user=args.user,
        ssh_password=password,
        udid=args.udid,
        device_port=args.device_port,
        renew_before_hours=args.renew_before_hours,
        setup_script=args.setup_script,
        iproxy_bin=args.iproxy,
        connect_timeout=args.timeout,
        ssh_identity=args.identity,
    )

    def handle_event(ev: DeployEvent) -> None:
        if args.json:
            print(ev.to_json(), flush=True)
        elif ev.event == "log":
            print(ev.message, flush=True)
        elif ev.event == "progress":
            pct = f"[{ev.percent}%] " if ev.percent is not None else ""
            print(f"{pct}{ev.message}", flush=True)
        elif ev.event == "done":
            print(ev.message, flush=True)
        elif ev.event == "error":
            print(f"Ошибка: {ev.message}", file=sys.stderr, flush=True)

    result = deploy_dopamine_renew(req, on_event=handle_event)
    return 0 if result.ok else (result.returncode or 1)


if __name__ == "__main__":
    raise SystemExit(main())
