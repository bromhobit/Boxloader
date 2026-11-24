#!/usr/bin/env python3
"""Build self-contained Boxloader AppImage from explicitly selected system runtimes.

Build host: CachyOS x86_64 with Python 3.14 and PySide6/Qt 6.
This is an experimental compatibility format for modern Linux distributions.
No credentials, private home files, or project work directories are copied.
"""
from __future__ import annotations
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from build_release import ROOT, VERSION, stage

PREFIX = Path('/usr/lib')
PY_VER = 'python3.14'
SITE = PREFIX / PY_VER / 'site-packages'
TOOL = Path.home() / '.cache/boxloader/appimagetool.AppImage'

def copy_stdlib(dst: Path) -> None:
    src=PREFIX/PY_VER
    def ignore(dir:str,names:list[str]) -> list[str]:
        skip={'site-packages','__pycache__','test','tests','idlelib','tkinter','turtledemo','ensurepip','pydoc_data'}
        return [n for n in names if n in skip or n.endswith(('.pyc','.pyo'))]
    shutil.copytree(src,dst,ignore=ignore)
    target=dst/'site-packages'
    target.mkdir(parents=True)
    for name in ['PySide6','shiboken6']:
        shutil.copytree(SITE/name,target/name,ignore=ignore)

def copy_qt(appdir: Path) -> None:
    libdir=appdir/'usr/lib'
    for name in ['libpython3.14.so.1.0','libpython3.14.so']:
        src=PREFIX/name
        if src.exists(): shutil.copy2(src,libdir/name)
    # Qt/PySide dynamic dependencies needed by the front-end.
    for pattern in ['libQt6*.so.6*','libpyside6*.so*','libshiboken6*.so*']:
        for lib in PREFIX.glob(pattern):
            if not lib.is_file(): continue
            dest=libdir/lib.name
            if not dest.exists(): shutil.copy2(lib,dest)
    src=PREFIX/'qt6/plugins'
    dst=libdir/'qt6/plugins'
    for name in ['platforms','platformthemes','styles','iconengines','imageformats','wayland-decoration-client','wayland-graphics-integration-client']:
        origin=src/name
        if origin.is_dir(): shutil.copytree(origin,dst/name)

def main():
    if not TOOL.is_file(): raise SystemExit(f'Please download appimagetool: {TOOL}')
    out=ROOT/'dist'/f'Boxloader-{VERSION}-x86_64.AppImage'
    out.parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='boxloader-appimage-') as tmp:
        appdir=Path(tmp)/'Boxloader.AppDir'
        appdir.mkdir()
        stage(appdir)
        copy_stdlib(appdir/'usr/lib'/PY_VER)
        copy_qt(appdir)
        shutil.copyfile(appdir/'usr/share/applications/boxloader.desktop',appdir/'boxloader.desktop')
        shutil.copyfile(appdir/'usr/share/icons/hicolor/256x256/apps/boxloader.png',appdir/'boxloader.png')
        apprun=appdir/'AppRun'
        apprun.write_text('''#!/bin/sh
set -eu
HERE="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
export PYTHONHOME="$HERE/usr"
export PYTHONPATH="$HERE/usr/lib/python3.14/site-packages"
export LD_LIBRARY_PATH="$HERE/usr/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export QT_PLUGIN_PATH="$HERE/usr/lib/qt6/plugins"
export QT_QPA_PLATFORM_PLUGIN_PATH="$HERE/usr/lib/qt6/plugins/platforms"
exec "$HERE/usr/bin/boxloader" "$@"
''')
        apprun.chmod(0o755)
        print('APPDIR BUILT',appdir,flush=True)
        subprocess.run([str(apprun),'--self-test'],cwd=appdir,check=True,timeout=22)
        print('APPDIR SELF-TEST PASSED',flush=True)
        env=os.environ.copy()
        env['ARCH']='x86_64'
        subprocess.run([str(TOOL),str(appdir),str(out)],env=env,check=True,timeout=300)
    print(f'BUILT {out} size={out.stat().st_size:,} bytes')
if __name__=='__main__':main()
