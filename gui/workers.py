"""Background QThread workers for USB, Apple ID, and SSH deploy operations.

All UI updates flow exclusively through Qt Signals (queued onto the GUI main thread).
Passwords and 2FA codes are never placed in CLI arguments or logged.
"""

from __future__ import annotations

import importlib.util
import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from PySide6.QtCore import QThread, Signal

from .protocol import CoreEvent, SecretSanitizer, parse_json_line, stage_label


def _drain_stream(stream: Any, bucket: list[str], on_line: Any = None) -> None:
    if stream is None:
        return
    try:
        for raw in stream:
            line = raw.rstrip("\r\n")
            if line:
                bucket.append(line)
                if on_line is not None:
                    on_line(line)
    except Exception:
        pass
    finally:
        try:
            stream.close()
        except Exception:
            pass


class CoreCommandWorker(QThread):
    """Runs a non-interactive dopamine-loader-core command and streams JSON-lines."""

    log_line = Signal(str)
    progress = Signal(str, int, str)  # stage, percent (-1 if unknown), message
    event_received = Signal(object)  # CoreEvent
    completed = Signal(bool, str, str, object)  # ok, message, raw_stdout, last_payload

    def __init__(
        self,
        core_bin: Path,
        args: list[str],
        *,
        stream_logs: bool = True,
        parent: Any = None,
    ) -> None:
        super().__init__(parent)
        self._core_bin = Path(core_bin)
        self._args = list(args)
        self._stream_logs = stream_logs
        self._cancel_event = threading.Event()
        self._proc_lock = threading.Lock()
        self._proc: subprocess.Popen[str] | None = None
        self._sanitizer = SecretSanitizer()

    def cancel(self) -> None:
        self._cancel_event.set()
        with self._proc_lock:
            proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass

    def run(self) -> None:
        if not self._core_bin.is_file():
            msg = f"Не найден бинарник ядра: {self._core_bin}"
            if self._stream_logs:
                self.log_line.emit(msg)
            self.completed.emit(False, msg, "", {})
            return

        cmd = [str(self._core_bin), *self._args]
        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        last_event: CoreEvent | None = None

        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            with self._proc_lock:
                self._proc = proc

            err_thread = threading.Thread(
                target=_drain_stream,
                args=(proc.stderr, stderr_lines),
                daemon=True,
            )
            err_thread.start()

            assert proc.stdout is not None
            for raw in proc.stdout:
                if self._cancel_event.is_set():
                    break
                line = raw.rstrip("\r\n")
                stdout_lines.append(line)
                ev = parse_json_line(line)
                if ev is None:
                    continue
                last_event = ev
                self.event_received.emit(ev)
                safe_msg = self._sanitizer.sanitize(ev.message)
                if ev.event == "progress":
                    pct = ev.percent if ev.percent is not None else -1
                    self.progress.emit(ev.stage, pct, safe_msg)
                    if self._stream_logs and safe_msg:
                        prefix = stage_label(ev.stage)
                        self.log_line.emit(f"[{prefix}] {safe_msg}" if prefix else safe_msg)
                elif ev.event in ("done", "error"):
                    if ev.event == "done" and ev.ok is not False:
                        self.progress.emit("done", 100, safe_msg or "Готово!")
                    if self._stream_logs and safe_msg:
                        self.log_line.emit(safe_msg)
                elif ev.event == "log" and self._stream_logs and safe_msg:
                    self.log_line.emit(safe_msg)

            if self._cancel_event.is_set() and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=3.0)
                except Exception:
                    try:
                        proc.kill()
                    except OSError:
                        pass

            rc = proc.wait()
            err_thread.join(timeout=2.0)
            try:
                proc.stdout.close()
            except Exception:
                pass

            raw_stdout = "\n".join(stdout_lines)
            if self._cancel_event.is_set():
                self.completed.emit(False, "Операция отменена пользователем.", raw_stdout, {})
                return

            if last_event is not None:
                ok = last_event.ok if last_event.ok is not None else (rc == 0)
                if rc != 0 and last_event.event == "error":
                    ok = False
                msg = self._sanitizer.sanitize(last_event.message)
                if not msg:
                    if ok:
                        msg = "Операция завершена успешно."
                    elif stderr_lines:
                        msg = self._sanitizer.sanitize(" ".join(stderr_lines[-3:]))
                    else:
                        msg = f"Команда завершилась с кодом {rc}."
                self.completed.emit(ok, msg, raw_stdout, dict(last_event.payload))
                return

            if rc == 0:
                self.completed.emit(True, "Операция завершена.", raw_stdout, {})
            else:
                err_txt = self._sanitizer.sanitize(" ".join(stderr_lines[-4:]))
                msg = err_txt or f"Ошибка выполнения ядра (код {rc})."
                if self._stream_logs:
                    self.log_line.emit(msg)
                self.completed.emit(False, msg, raw_stdout, {})
        except Exception as exc:
            msg = self._sanitizer.sanitize(f"Не удалось запустить ядро: {exc}")
            if self._stream_logs:
                self.log_line.emit(msg)
            self.completed.emit(False, msg, "", {})
        finally:
            with self._proc_lock:
                proc_ref = self._proc
                self._proc = None
            if proc_ref is not None:
                for st in (proc_ref.stdin, proc_ref.stdout, proc_ref.stderr):
                    if st is not None:
                        try:
                            st.close()
                        except Exception:
                            pass
            self._sanitizer.clear()


class AppleLoginWorker(QThread):
    """Interactive Apple ID login worker communicating password & 2FA solely via stdin."""

    log_line = Signal(str)
    progress = Signal(str, int, str)
    need_2fa = Signal(str, bool, bool, str)  # message, retry, sms, last_error
    completed = Signal(bool, str, str)  # ok, message, email_masked

    def __init__(
        self,
        core_bin: Path,
        session_dir: Path,
        email: str,
        password: str,
        *,
        overwrite: bool = False,
        parent: Any = None,
    ) -> None:
        super().__init__(parent)
        self._core_bin = Path(core_bin)
        self._session_dir = Path(session_dir)
        self._email = email.strip()
        self._password = password
        self._overwrite = overwrite
        self._code_queue: queue.Queue[str] = queue.Queue()
        self._cancel_event = threading.Event()
        self._proc_lock = threading.Lock()
        self._proc: subprocess.Popen[str] | None = None
        self._sanitizer = SecretSanitizer()

    def submit_2fa_code(self, code: str) -> None:
        """Send 6-digit code, 'resend', or '' (cancel) to the waiting login worker."""
        self._code_queue.put(code)

    def cancel(self) -> None:
        self._cancel_event.set()
        self._code_queue.put("")
        with self._proc_lock:
            proc = self._proc
            if proc is not None and proc.poll() is None:
                try:
                    proc.terminate()
                except (OSError, ValueError):
                    pass

    def run(self) -> None:
        password = self._password
        self._password = ""
        self._sanitizer.add_transient(password)

        if not self._core_bin.is_file():
            self._sanitizer.clear()
            self.completed.emit(False, f"Не найден бинарник ядра: {self._core_bin}", "")
            return

        cmd = [
            str(self._core_bin),
            "login",
            "--session-dir",
            str(self._session_dir),
            "--email",
            self._email,
            "--json",
        ]
        if self._overwrite:
            cmd.append("--overwrite")

        stderr_lines: list[str] = []
        last_event: CoreEvent | None = None

        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            with self._proc_lock:
                self._proc = proc

            err_thread = threading.Thread(
                target=_drain_stream,
                args=(proc.stderr, stderr_lines),
                daemon=True,
            )
            err_thread.start()

            # Write password strictly via stdin pipe, never argv or env.
            assert proc.stdin is not None
            try:
                proc.stdin.write(password + "\n")
                proc.stdin.flush()
            finally:
                password = ""

            assert proc.stdout is not None
            for raw in proc.stdout:
                if self._cancel_event.is_set():
                    break
                ev = parse_json_line(raw)
                if ev is None:
                    continue
                last_event = ev
                safe_msg = self._sanitizer.sanitize(ev.message)

                if ev.event == "progress":
                    pct = ev.percent if ev.percent is not None else -1
                    self.progress.emit(ev.stage, pct, safe_msg)
                    if safe_msg:
                        self.log_line.emit(f"[Apple ID] {safe_msg}")
                elif ev.event == "need_2fa":
                    retry = bool(ev.payload.get("retry", False))
                    sms = bool(ev.payload.get("sms", False))
                    last_err = self._sanitizer.sanitize(str(ev.payload.get("last_error") or ""))
                    prompt = safe_msg or "Введите 6-значный код подтверждения Apple ID"
                    self.log_line.emit("[Apple ID] Требуется код двухфакторной аутентификации (2FA).")
                    self.need_2fa.emit(prompt, retry, sms, last_err)

                    code = self._code_queue.get()
                    if self._cancel_event.is_set() or not code.strip():
                        try:
                            if not proc.stdin.closed:
                                proc.stdin.write("\n")
                                proc.stdin.flush()
                                proc.stdin.close()
                        except (OSError, ValueError, RuntimeError):
                            pass
                        break

                    cleaned = code.strip()
                    if cleaned.lower() != "resend":
                        self._sanitizer.add_transient(cleaned)
                    try:
                        proc.stdin.write(cleaned + "\n")
                        proc.stdin.flush()
                    except (OSError, ValueError, RuntimeError):
                        break
                    cleaned = ""
                elif ev.event in ("done", "error"):
                    if safe_msg:
                        self.log_line.emit(f"[Apple ID] {safe_msg}")

            try:
                if proc.stdin and not proc.stdin.closed:
                    proc.stdin.close()
            except (OSError, ValueError, RuntimeError):
                pass

            if self._cancel_event.is_set() and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=3.0)
                except Exception:
                    try:
                        proc.kill()
                    except OSError:
                        pass

            rc = proc.wait()
            err_thread.join(timeout=2.0)
            try:
                proc.stdout.close()
            except Exception:
                pass

            if self._cancel_event.is_set():
                self.completed.emit(False, "Вход в Apple ID отменён.", "")
                return

            if last_event is not None:
                ok = (last_event.event == "done" and last_event.ok is not False) and rc == 0
                msg = self._sanitizer.sanitize(last_event.message)
                email_masked = str(last_event.payload.get("email_masked") or "")
                if not msg:
                    msg = "Вход выполнен." if ok else f"Не удалось войти (код {rc})."
                self.completed.emit(ok, msg, email_masked)
                return

            err_txt = self._sanitizer.sanitize(" ".join(stderr_lines[-3:]))
            self.completed.emit(
                rc == 0,
                err_txt or ("Вход выполнен." if rc == 0 else f"Ошибка авторизации (код {rc})."),
                "",
            )
        except Exception as exc:
            msg = self._sanitizer.sanitize(f"Ошибка процесса входа: {exc}")
            self.log_line.emit(msg)
            self.completed.emit(False, msg, "")
        finally:
            password = ""
            with self._proc_lock:
                proc_ref = self._proc
                self._proc = None
            if proc_ref is not None:
                for st in (proc_ref.stdin, proc_ref.stdout, proc_ref.stderr):
                    if st is not None:
                        try:
                            st.close()
                        except Exception:
                            pass
            self._sanitizer.clear()


def _load_deploy_module(deploy_module_path: Path) -> Any:
    """Import dopamine_deploy.py from explicit path or launcher package."""
    default_path = Path(__file__).resolve().parent.parent / "launcher" / "dopamine_deploy.py"
    resolved = deploy_module_path.resolve() if deploy_module_path.exists() else deploy_module_path

    if resolved == default_path.resolve():
        try:
            from launcher import dopamine_deploy  # type: ignore[import-not-found]
            return dopamine_deploy
        except Exception:
            pass

    if not deploy_module_path.is_file():
        raise FileNotFoundError(f"Модуль развёртывания не найден: {deploy_module_path}")

    launcher_dir = str(deploy_module_path.parent)
    if launcher_dir not in sys.path:
        sys.path.insert(0, launcher_dir)

    mod_name = f"_dopamine_deploy_{abs(hash(str(resolved)))}"
    spec = importlib.util.spec_from_file_location(mod_name, str(deploy_module_path))
    if spec is None or spec.loader is None:
        raise ImportError(f"Не удалось загрузить {deploy_module_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


class DopamineDeployWorker(QThread):
    """Runs launcher/dopamine_deploy.py in a background thread with cancellation."""

    log_line = Signal(str)
    progress = Signal(str, int, str)  # stage, percent (-1 if unknown), message
    completed = Signal(bool, str)

    def __init__(
        self,
        deploy_module_path: Path,
        *,
        deb_path: Path | None,
        profile_paths: list[Path],
        session_dir: Path | None,
        ssh_user: str,
        ssh_password: str,
        udid: str | None,
        setup_script: Path,
        parent: Any = None,
    ) -> None:
        super().__init__(parent)
        self._deploy_module_path = Path(deploy_module_path)
        self._deb_path = deb_path
        self._profile_paths = list(profile_paths)
        self._session_dir = session_dir
        self._ssh_user = ssh_user.strip() or "root"
        self._ssh_password = ssh_password
        self._udid = udid
        self._setup_script = Path(setup_script)
        self._cancel_event = threading.Event()
        self._sanitizer = SecretSanitizer()

    def cancel(self) -> None:
        self._cancel_event.set()

    def run(self) -> None:
        ssh_password = self._ssh_password
        self._ssh_password = ""
        self._sanitizer.add_transient(ssh_password)

        try:
            mod = _load_deploy_module(self._deploy_module_path)
            request = mod.DeployRequest(
                deb_path=self._deb_path,
                profile_paths=self._profile_paths,
                session_dir=self._session_dir,
                ssh_user=self._ssh_user,
                ssh_password=ssh_password,
                udid=self._udid,
                setup_script=self._setup_script,
            )
            ssh_password = ""

            def _on_event(ev: Any) -> None:
                ev_kind = str(getattr(ev, "event", "log"))
                stage = str(getattr(ev, "stage", ""))
                pct_raw = getattr(ev, "percent", None)
                pct = int(pct_raw) if isinstance(pct_raw, (int, float)) else -1
                safe_msg = self._sanitizer.sanitize(str(getattr(ev, "message", "")))

                if ev_kind == "progress":
                    self.progress.emit(stage, pct, safe_msg)
                    if safe_msg:
                        prefix = stage_label(stage)
                        self.log_line.emit(f"[{prefix}] {safe_msg}" if prefix else safe_msg)
                elif ev_kind in ("log", "done", "error"):
                    if ev_kind == "done":
                        self.progress.emit("done", 100, safe_msg or "Установка завершена!")
                    if safe_msg:
                        self.log_line.emit(safe_msg)

            result = mod.deploy_dopamine_renew(
                request,
                on_event=_on_event,
                cancel_event=self._cancel_event,
            )
            ok = bool(getattr(result, "ok", False))
            msg = self._sanitizer.sanitize(str(getattr(result, "message", "")))
            if not msg:
                msg = (
                    "Dopamine Renew успешно установлен на iPhone."
                    if ok
                    else "Установка Dopamine Renew завершилась с ошибкой."
                )
            self.completed.emit(ok, msg)
        except Exception as exc:
            msg = self._sanitizer.sanitize(f"Ошибка установки Dopamine Renew: {exc}")
            self.log_line.emit(msg)
            self.completed.emit(False, msg)
        finally:
            ssh_password = ""
            self._sanitizer.clear()
