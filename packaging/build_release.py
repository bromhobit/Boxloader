#!/usr/bin/env python3
"""Reproducible local packaging for Boxloader: Arch and Debian system packages.

No user state, pairing records, provisioning profiles or secrets are read.
Only a strict allow-list of source/UI files and explicitly built ELF binaries
is included. Requires Linux host with Rust, Python 3, GNU ar and zstd.
"""
from __future__ import annotations
import argparse
import gzip
import io
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.2.2"
RELEASE = "1"
NAME = "boxloader"
CORE = ROOT / "core/target/release/dopamine-loader-core"
LAUNCHER = ROOT / "native-launcher/target/release/boxloader"

def command(*argv: str, cwd: Path | None = None) -> None:
    subprocess.run(argv, cwd=cwd, check=True)

def build() -> None:
    for crate in ("core", "native-launcher"):
        command("cargo", "build", "--release", "--locked", "--manifest-path", str(ROOT / crate / "Cargo.toml"))

def stage(root: Path) -> None:
    def put(source: Path, relative: str, mode: int) -> None:
        if not source.is_file() or source.is_symlink():
            raise RuntimeError(f"Source is missing or unsafe: {source}")
        dest = root / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)
        os.chmod(dest, mode)
    put(LAUNCHER, "usr/bin/boxloader", 0o755)
    put(CORE, "usr/libexec/boxloader/dopamine-loader-core", 0o755)
    put(ROOT / "launcher/icon.png", "usr/share/icons/hicolor/256x256/apps/boxloader.png", 0o644)
    put(ROOT / "launcher/icon.png", "usr/share/boxloader/app/launcher/icon.png", 0o644)
    for source in sorted((ROOT / "gui").glob("*.py")):
        put(source, f"usr/share/boxloader/app/gui/{source.name}", 0o644)
    entry = (ROOT / "Boxloader.desktop").read_text(encoding="utf-8")
    entry = entry.replace("Exec=boxloader", "Exec=/usr/bin/boxloader")
    entry = entry.replace("Icon=boxloader", "Icon=boxloader")
    dest = root / "usr/share/applications/boxloader.desktop"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(entry, encoding="utf-8")
    dest.chmod(0o644)

def tarbytes(root: Path, extra: dict[str, bytes] | None = None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.GNU_FORMAT) as tar:
        for path in sorted(root.rglob("*")):
            rel = path.relative_to(root).as_posix()
            info = tar.gettarinfo(str(path), arcname=rel)
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            info.mtime = 0
            if path.is_file():
                with path.open("rb") as f:
                    tar.addfile(info, f)
            elif path.is_dir():
                tar.addfile(info)
        for name, payload in sorted((extra or {}).items()):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mode = 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            info.mtime = 0
            tar.addfile(info, io.BytesIO(payload))
    return buf.getvalue()

def make_arch(data: Path, output: Path) -> None:
    size = sum(f.stat().st_size for f in data.rglob("*") if f.is_file())
    meta = f"""pkgname = {NAME}
pkgbase = {NAME}
pkgver = {VERSION}-{RELEASE}
pkgdesc = Linux GUI for USB IPA installation on iPhone (Rust core + PySide6)
url = https://github.com/bromhobit/Boxloader
builddate = {int(time.time())}
packager = Boxloader release
size = {size}
arch = x86_64
license = custom
depend = glibc
depend = python>=3.14
depend = pyside6
depend = libusbmuxd
depend = usbmuxd
"""
    with tempfile.TemporaryDirectory() as tmp:
        plain = Path(tmp) / "package.tar"
        plain.write_bytes(tarbytes(data, {".PKGINFO": meta.encode()}))
        command("zstd", "-q", "-T0", "-10", "-f", str(plain), "-o", str(output))

def gz(data: bytes) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as z:
        z.write(data)
    return buf.getvalue()

def make_deb(data: Path, output: Path) -> None:
    # Debian requires CPython 3.14 and PySide6 QtWidgets installed separately;
    # explicit dependencies prevent a silently broken installation.
    size_kib = (sum(f.stat().st_size for f in data.rglob("*") if f.is_file()) + 1023)//1024
    control = f"""Package: {NAME}
Version: {VERSION}-{RELEASE}
Section: utils
Priority: optional
Architecture: amd64
Maintainer: Boxloader <noreply@github.com>
Installed-Size: {size_kib}
Depends: python3 (>= 3.14), libpython3.14, python3-pyside6.qtwidgets, libc6
Description: Rust-based USB IPA installer for iPhone with PySide6 GUI
 Boxloader installs IPA apps using an existing Apple ID session.
 Requires Python 3.14 and the PySide6 QtWidgets modules at runtime.
""".encode()
    with tempfile.TemporaryDirectory() as tmp:
        temp = Path(tmp)
        (temp / "debian-binary").write_bytes(b"2.0\n")
        empty = temp / "empty"
        empty.mkdir()
        (temp / "control.tar.gz").write_bytes(gz(tarbytes(empty, {"control": control})))
        (temp / "data.tar.gz").write_bytes(gz(tarbytes(data)))
        output.unlink(missing_ok=True)
        command("ar", "qc", str(output), "debian-binary", "control.tar.gz", "data.tar.gz", cwd=temp)

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-build", action="store_true", help="Use previously built release ELF binaries")
    args = parser.parse_args()
    if not args.no_build:
        build()
    out = ROOT / "dist"
    out.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        stage_dir = Path(tmp) / "stage"
        stage_dir.mkdir()
        stage(stage_dir)
        arch = out / f"boxloader-{VERSION}-{RELEASE}-x86_64.pkg.tar.zst"
        deb = out / f"boxloader_{VERSION}-{RELEASE}_amd64.deb"
        make_arch(stage_dir, arch)
        make_deb(stage_dir, deb)
        for p in (arch, deb):
            print(f"BUILT {p} ({p.stat().st_size:,} bytes)")

if __name__ == "__main__":
    main()
