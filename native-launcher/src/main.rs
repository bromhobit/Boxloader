//! Native Boxloader executable. Embeds the system CPython interpreter in this
//! Rust process: the KDE .desktop entry executes this ELF directly, not a shell
//! or Python launcher script. The PySide6 front-end is kept as Python modules.
use std::{env, ffi::CString, path::PathBuf, process::ExitCode};

#[link(name = "python3.14")]
unsafe extern "C" {
    fn Py_Initialize();
    fn Py_IsInitialized() -> i32;
    fn PyRun_SimpleStringFlags(command: *const std::ffi::c_char, flags: *mut std::ffi::c_void) -> i32;
    fn Py_FinalizeEx() -> i32;
}

fn home_dir() -> Result<PathBuf, String> {
    env::var_os("HOME")
        .filter(|x| !x.is_empty())
        .map(PathBuf::from)
        .ok_or("HOME environment variable is missing".into())
}

fn run() -> Result<(), String> {
    let args: Vec<String> = env::args().skip(1).collect();
    if args.iter().any(|arg| arg == "--help" || arg == "-h") {
        println!("Boxloader — native Linux launcher (Rust + embedded CPython 3.14 / PySide6)\nUsage: boxloader [--version|--self-test|--help]");
        return Ok(());
    }
    if args.iter().any(|arg| arg == "--version") {
        println!("Boxloader 0.2.2");
        return Ok(());
    }
    let self_test = args.iter().any(|arg| arg == "--self-test");
    if args.iter().any(|arg| !matches!(arg.as_str(), "--self-test")) {
        return Err("Unsupported argument (use --help)".into());
    }

    let home = home_dir()?;
    // Discover resources relative to the installed ELF. Works for user-local
    // installation, /usr system packages and unpacked portable release tarballs.
    let exe = env::current_exe().map_err(|e| format!("Could not locate executable: {e}"))?;
    let prefix = exe.parent().and_then(|dir| dir.parent())
        .ok_or("Cannot determine the Boxloader installation prefix")?;
    let app_dir = prefix.join("share/boxloader/app");
    let core = prefix.join("libexec/boxloader/dopamine-loader-core");
    if !app_dir.join("gui/app.py").is_file() {
        return Err(format!("Boxloader modules not installed: {}", app_dir.display()));
    }
    if !core.is_file() {
        return Err(format!("Boxloader USB core not installed: {}", core.display()));
    }
    // Explicit paths to user-installed resources, no shell interpolation.
    unsafe {
        env::set_var("BOXLOADER_APP_ROOT", &app_dir);
        env::set_var("DOPAMINE_LOADER_CORE", &core);
        env::set_var("DOPAMINE_ICON_PATH", app_dir.join("launcher/icon.png"));
    }
    if env::var_os("DOPAMINE_OUTPUTS_DIR").is_none() {
        unsafe { env::set_var("DOPAMINE_OUTPUTS_DIR", home.join("Downloads")); }
    }
    if env::var_os("DOPAMINE_SESSION_DIR").is_none() {
        // App data lives in the user's private XDG state directory, never in
        // source trees or temporary Codex workspaces. Existing sessions must
        // be migrated separately; the launcher never copies credentials.
        let state_root = env::var_os("XDG_STATE_HOME")
            .map(PathBuf::from)
            .filter(|p| p.is_absolute())
            .unwrap_or_else(|| home.join(".local/state"));
        unsafe { env::set_var("DOPAMINE_SESSION_DIR", state_root.join("boxloader/apple-session")); }
    }
    if self_test { unsafe { env::set_var("QT_QPA_PLATFORM", "offscreen") }; }

    const NORMAL: &str = r#"
import os, sys
sys.argv = ['boxloader']
sys.path.insert(0, os.environ['BOXLOADER_APP_ROOT'])
from gui.app import main
main()
"#;
    const SELF_TEST: &str = r#"
import os, sys
sys.argv = ['boxloader']
sys.path.insert(0, os.environ['BOXLOADER_APP_ROOT'])
from PySide6.QtWidgets import QApplication
from gui.app import BoxloaderWindow
from gui.config import AppPaths
app = QApplication([])
window = BoxloaderWindow(paths=AppPaths.discover(), auto_refresh=False)
assert window.tabs.count() == 2
assert 'IPA' in window.tabs.tabText(0)
assert 'Журнал' in window.tabs.tabText(1)
assert not hasattr(window, 'renew_usb_btn')
assert not hasattr(window, 'deploy_btn')
assert not hasattr(window, 'pairing_btn')
assert window.paths.core_bin.is_file()
window.close()
print('Boxloader native UI self-test: OK (IPA + Journal only)', flush=True)
"#;
    let code = CString::new(if self_test { SELF_TEST } else { NORMAL })
        .map_err(|_| "Embedded Python snippet contains NUL".to_string())?;
    let result = unsafe {
        Py_Initialize();
        if Py_IsInitialized() == 0 {
            return Err("Could not initialize embedded CPython".into());
        }
        let status = PyRun_SimpleStringFlags(code.as_ptr(), std::ptr::null_mut());
        let finalized = Py_FinalizeEx();
        (status, finalized)
    };
    if result.0 != 0 {
        return Err("Boxloader Python GUI raised an exception (see traceback above)".into());
    }
    if result.1 != 0 {
        return Err("CPython shutdown failed".into());
    }
    Ok(())
}

fn main() -> ExitCode {
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(err) => { eprintln!("Boxloader: {err}"); ExitCode::FAILURE }
    }
}
