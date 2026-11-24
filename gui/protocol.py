"""JSON-lines protocol parsing, state models, and secret redaction for DopamineLoader."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

INSTALL_STAGES: tuple[tuple[str, str], ...] = (
    ("connecting", "Подключение"),
    ("authenticating", "Авторизация"),
    ("signing", "Подпись"),
    ("uploading", "Загрузка"),
    ("installing", "Установка"),
    ("done", "Готово!"),
)

_STAGE_LABELS: dict[str, str] = {
    "connecting": "Подключение к iPhone",
    "authenticating": "Авторизация в Apple ID",
    "signing": "Подпись приложения",
    "uploading": "Загрузка на iPhone",
    "installing": "Установка на iPhone",
    "verifying": "Проверка установки",
    "iproxy": "Запуск USB-туннеля (iproxy)",
    "ssh": "Подключение по USB SSH",
    "deploy": "Установка Dopamine Renew",
    "cleanup": "Завершение и очистка",
    "done": "Готово!",
}

_KV_SECRET_RE = re.compile(
    r"(?i)\b(password|passwd|passphrase|token|secret|spd|myacinfo|x-apple-gs-token|gs_token|adsid|pet|cookie)\b"
    r"(\s*[:=]\s*)([^\s,;'\")}\]]+)"
)
_JSON_SECRET_RE = re.compile(
    r'(?i)("(?:password|passwd|passphrase|token|secret|spd|myacinfo|x-apple-gs-token|gs_token|adsid|pet|cookie)"\s*:\s*)"[^"]*"'
)


class SecretSanitizer:
    """Redacts active transient secrets and known token patterns before logging."""

    def __init__(self) -> None:
        self._transient: list[str] = []

    def add_transient(self, secret: str) -> None:
        s = secret.strip()
        if len(s) >= 3 and s not in self._transient:
            self._transient.append(s)

    def clear(self) -> None:
        self._transient.clear()

    def sanitize(self, text: str) -> str:
        if not text:
            return ""
        out = text
        for sec in self._transient:
            if sec and sec in out:
                out = out.replace(sec, "[СКРЫТО]")
        out = _JSON_SECRET_RE.sub(r'\1"[СКРЫТО]"', out)
        out = _KV_SECRET_RE.sub(r"\1\2[СКРЫТО]", out)
        return out


def stage_label(stage: str) -> str:
    key = (stage or "").strip().lower()
    if not key:
        return ""
    return _STAGE_LABELS.get(key, stage)


def stage_step_index(stage: str) -> int:
    key = (stage or "").strip().lower()
    for idx, (code, _) in enumerate(INSTALL_STAGES):
        if code == key:
            return idx
    if key == "verifying":
        return 4
    return -1


@dataclass(frozen=True)
class DeviceInfo:
    udid: str
    device_id: int | None = None
    name: str = ""
    product_type: str = ""
    product_version: str = ""
    paired: bool = False

    def short_udid(self) -> str:
        if len(self.udid) <= 16:
            return self.udid
        return f"{self.udid[:8]}…{self.udid[-6:]}"

    def display_label(self) -> str:
        title = self.name.strip() or self.product_type.strip() or "iPhone"
        ios_part = f" (iOS {self.product_version})" if self.product_version else ""
        pair_part = "✓ Пара" if self.paired else "Нет пары"
        return f"{title}{ios_part} · {self.short_udid()} · {pair_part}"


@dataclass(frozen=True)
class SessionStatus:
    exists: bool
    valid: bool
    email_masked: str | None = None
    message: str = ""


@dataclass(frozen=True)
class CoreEvent:
    event: str
    stage: str = ""
    percent: int | None = None
    message: str = ""
    ok: bool | None = None
    payload: dict[str, Any] = field(default_factory=dict)


def parse_json_line(raw_line: str) -> CoreEvent | None:
    """Parse one stdout line from dopamine-loader-core into a CoreEvent."""
    line = raw_line.strip()
    if not line:
        return None
    if not line.startswith("{"):
        return CoreEvent(event="log", message=line)
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return CoreEvent(event="log", message=line)
    if not isinstance(obj, dict):
        return CoreEvent(event="log", message=line)

    event_type = str(obj.get("event") or "").strip().lower()
    if not event_type:
        if "devices" in obj:
            event_type = "devices"
        elif "valid" in obj and "exists" in obj:
            event_type = "session_status"
        elif obj.get("ok") is False or obj.get("error"):
            event_type = "error"
        else:
            event_type = "done"

    stage = str(obj.get("stage") or "")
    pct_val = obj.get("percent")
    percent: int | None = None
    if isinstance(pct_val, (int, float)) and not isinstance(pct_val, bool):
        percent = max(0, min(100, int(pct_val)))

    msg = str(obj.get("message") or obj.get("error") or "")
    ok_raw = obj.get("ok")
    ok = ok_raw if isinstance(ok_raw, bool) else None
    if ok is None:
        if event_type == "error":
            ok = False
        elif event_type == "done":
            ok = True

    return CoreEvent(
        event=event_type,
        stage=stage,
        percent=percent,
        message=msg,
        ok=ok,
        payload=obj,
    )


def _device_from_dict(item: dict[str, Any]) -> DeviceInfo | None:
    udid = str(item.get("udid") or "").strip()
    if not udid:
        return None
    dev_id = item.get("device_id")
    device_id = int(dev_id) if isinstance(dev_id, int) and not isinstance(dev_id, bool) else None
    return DeviceInfo(
        udid=udid,
        device_id=device_id,
        name=str(item.get("name") or ""),
        product_type=str(item.get("product_type") or ""),
        product_version=str(item.get("product_version") or ""),
        paired=bool(item.get("paired", False)),
    )


def parse_devices_output(raw_stdout: str) -> tuple[list[DeviceInfo], str | None]:
    """Parse `dopamine-loader-core devices --json` stdout into (devices, error_message)."""
    text = raw_stdout.strip()
    if not text:
        return [], None

    # Try full-document JSON first, then line-by-line JSON-lines.
    candidates: list[Any] = []
    try:
        candidates.append(json.loads(text))
    except json.JSONDecodeError:
        for ln in text.splitlines():
            ln = ln.strip()
            if ln.startswith("{") or ln.startswith("["):
                try:
                    candidates.append(json.loads(ln))
                except json.JSONDecodeError:
                    continue

    devices: list[DeviceInfo] = []
    err_msg: str | None = None
    for obj in candidates:
        if isinstance(obj, list):
            for entry in obj:
                if isinstance(entry, dict):
                    dev = _device_from_dict(entry)
                    if dev:
                        devices.append(dev)
        elif isinstance(obj, dict):
            if obj.get("event") == "error" or obj.get("ok") is False:
                err_msg = str(obj.get("message") or obj.get("error") or "Ошибка usbmuxd")
            raw_list = obj.get("devices")
            if isinstance(raw_list, list):
                for entry in raw_list:
                    if isinstance(entry, dict):
                        dev = _device_from_dict(entry)
                        if dev:
                            devices.append(dev)
    return devices, err_msg


def parse_session_status_output(raw_stdout: str) -> SessionStatus | None:
    """Parse `dopamine-loader-core session-status --json` stdout."""
    text = raw_stdout.strip()
    if not text:
        return None

    objs: list[dict[str, Any]] = []
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            objs.append(parsed)
    except json.JSONDecodeError:
        for ln in text.splitlines():
            ln = ln.strip()
            if ln.startswith("{"):
                try:
                    parsed = json.loads(ln)
                    if isinstance(parsed, dict):
                        objs.append(parsed)
                except json.JSONDecodeError:
                    continue

    for obj in reversed(objs):
        if "exists" in obj or "valid" in obj:
            email_val = obj.get("email_masked")
            email_masked = str(email_val).strip() if email_val else None
            msg = str(obj.get("message") or obj.get("error") or "")
            return SessionStatus(
                exists=bool(obj.get("exists", False)),
                valid=bool(obj.get("valid", False)),
                email_masked=email_masked,
                message=msg,
            )
        if obj.get("event") == "error" or obj.get("ok") is False:
            return SessionStatus(
                exists=False,
                valid=False,
                email_masked=None,
                message=str(obj.get("message") or obj.get("error") or ""),
            )
    return None
