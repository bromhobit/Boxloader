"""Path resolution, IPA discovery, and .mobileprovision metadata helpers."""

from __future__ import annotations

import os
import plistlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Keep state, binaries and IPA discovery independent from the development
# checkout and from the Codex workspace used during the first prototype.
DEFAULT_OUTPUTS_DIR = Path.home() / "Downloads"
DEFAULT_STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "boxloader"
DEFAULT_WORK_DIR = DEFAULT_STATE_DIR
DEFAULT_CORE_BIN = Path.home() / ".local/libexec/boxloader/dopamine-loader-core"


@dataclass
class AppPaths:
    outputs_dir: Path
    work_dir: Path
    core_bin: Path
    setup_script: Path
    deploy_module: Path
    session_dir: Path
    default_deb: Path
    default_profile: Path
    icon_path: Path

    @classmethod
    def discover(
        cls,
        *,
        env: dict[str, str] | None = None,
        outputs_override: str | Path | None = None,
        work_override: str | Path | None = None,
        core_override: str | Path | None = None,
        setup_script_override: str | Path | None = None,
        session_override: str | Path | None = None,
        icon_override: str | Path | None = None,
    ) -> "AppPaths":
        env_map = os.environ if env is None else env

        outputs_dir = Path(
            outputs_override
            or env_map.get("DOPAMINE_OUTPUTS_DIR")
            or DEFAULT_OUTPUTS_DIR
        )
        work_dir = Path(
            work_override
            or env_map.get("DOPAMINE_WORK_DIR")
            or DEFAULT_WORK_DIR
        )
        core_bin = Path(
            core_override
            or env_map.get("DOPAMINE_LOADER_CORE")
            or DEFAULT_CORE_BIN
        )
        setup_script = Path(
            setup_script_override
            or env_map.get("DOPAMINE_SETUP_SCRIPT")
            or (outputs_dir / "dopamine-renew" / "package" / "setup-device.sh")
        )
        deploy_module = PROJECT_ROOT / "launcher" / "dopamine_deploy.py"
        session_dir = Path(
            session_override
            or env_map.get("DOPAMINE_SESSION_DIR")
            or (work_dir / "apple-session")
        )
        default_deb = outputs_dir / "DopamineRenew-0.2.0-rootless.deb"
        if not default_deb.is_file():
            # Fall back to newest matching rootless deb if version changes.
            debs = sorted(outputs_dir.glob("DopamineRenew-*-rootless.deb"), reverse=True)
            if debs:
                default_deb = debs[0]
        default_profile = session_dir / "sidestore-fresh.mobileprovision"
        if not default_profile.is_file() and session_dir.is_dir():
            provs = sorted(session_dir.glob("*.mobileprovision"))
            if provs:
                default_profile = provs[0]

        icon_candidates = [
            icon_override,
            env_map.get("DOPAMINE_ICON_PATH"),
            PROJECT_ROOT / "launcher" / "icon.png",
            outputs_dir / "dopamine-renew" / "ios-app" / "AppIcon60x60@3x.png",
        ]
        icon_path = PROJECT_ROOT / "launcher" / "icon.png"
        for cand in icon_candidates:
            if cand:
                p = Path(cand)
                if p.is_file():
                    icon_path = p
                    break

        return cls(
            outputs_dir=outputs_dir,
            work_dir=work_dir,
            core_bin=core_bin,
            setup_script=setup_script,
            deploy_module=deploy_module,
            session_dir=session_dir,
            default_deb=default_deb,
            default_profile=default_profile,
            icon_path=icon_path,
        )


@dataclass(frozen=True)
class IpaCandidate:
    path: Path
    name: str
    size_bytes: int
    is_trollstore_only: bool
    is_featured: bool = False

    @property
    def size_label(self) -> str:
        mb = self.size_bytes / (1024.0 * 1024.0)
        if mb >= 1.0:
            return f"{mb:.1f} МБ"
        kb = self.size_bytes / 1024.0
        return f"{kb:.0f} КБ"


_VER_RE = re.compile(r"(\d+)")


def _natural_desc_key(path: Path) -> tuple:
    # Sort featured SideBox IPAs first, then normal sideloadable IPAs, then TrollStore-only IPAs, newest first.
    stem = path.stem.lower()
    is_sidebox = stem.startswith("sidebox")
    is_ts = stem.startswith("dopaminerenew")
    tier = 0 if is_sidebox else (2 if is_ts else 1)
    nums = tuple(int(m.group(1)) for m in _VER_RE.finditer(stem))
    # Negate version tuples so higher versions sort first.
    neg_nums = tuple(-n for n in nums)
    prefix = re.sub(r"[\d.\-_]+$", "", stem)
    return (tier, prefix, neg_nums, stem)


def discover_ipa_files(outputs_dir: Path) -> list[IpaCandidate]:
    """Return .ipa files in outputs_dir sorted for quick-select chips."""
    if not outputs_dir.is_dir():
        return []
    found: list[Path] = []
    try:
        for entry in outputs_dir.iterdir():
            if entry.is_file() and entry.suffix.lower() == ".ipa":
                found.append(entry)
    except OSError:
        return []

    found.sort(key=_natural_desc_key)
    candidates: list[IpaCandidate] = []
    for p in found:
        try:
            sz = p.stat().st_size
        except OSError:
            sz = 0
        lower = p.name.lower()
        is_ts = lower.startswith("dopaminerenew")
        is_featured = lower.startswith("sidebox")
        candidates.append(
            IpaCandidate(
                path=p,
                name=p.name,
                size_bytes=sz,
                is_trollstore_only=is_ts,
                is_featured=is_featured,
            )
        )
    return candidates


@dataclass(frozen=True)
class ProvisionMetadata:
    app_name: str
    bundle_id: str
    team_id: str
    expiration_utc: str
    device_count: int


def parse_mobileprovision_metadata(path: Path) -> ProvisionMetadata | None:
    """Safely extract non-secret metadata from a CMS-wrapped .mobileprovision file."""
    try:
        if not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
            return None
        raw = path.read_bytes()
    except OSError:
        return None

    start = raw.find(b"<?xml")
    end = raw.find(b"</plist>")
    if start < 0 or end < 0 or end <= start:
        return None

    xml_bytes = raw[start : end + len(b"</plist>")]
    try:
        data = plistlib.loads(xml_bytes)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None

    ent = data.get("Entitlements")
    app_identifier = ""
    if isinstance(ent, dict):
        app_identifier = str(ent.get("application-identifier") or "")

    team_ids = data.get("TeamIdentifier")
    team_id = ""
    if isinstance(team_ids, list) and team_ids:
        team_id = str(team_ids[0])
    elif "." in app_identifier:
        team_id = app_identifier.split(".", 1)[0]

    bundle_id = ""
    if "." in app_identifier:
        bundle_id = app_identifier.split(".", 1)[1]

    app_name = str(data.get("AppIDName") or data.get("Name") or bundle_id or path.stem)

    exp = data.get("ExpirationDate")
    exp_str = "неизвестно"
    if isinstance(exp, datetime):
        dt = exp if exp.tzinfo is not None else exp.replace(tzinfo=timezone.utc)
        exp_str = dt.astimezone().strftime("%d.%m.%Y %H:%M")

    devices = data.get("ProvisionedDevices")
    dev_count = len(devices) if isinstance(devices, Iterable) and not isinstance(devices, (str, bytes)) else 0

    return ProvisionMetadata(
        app_name=app_name,
        bundle_id=bundle_id,
        team_id=team_id,
        expiration_utc=exp_str,
        device_count=dev_count,
    )
