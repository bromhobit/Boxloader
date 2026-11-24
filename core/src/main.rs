mod profile;
mod protocol;
mod session;
mod usb;

use protocol::{Emitter, StreamEvent, print_json_value};
use std::{env, path::PathBuf, process::ExitCode};
use usb::{CertPolicy, ExtensionPolicy, PairingTarget};

#[derive(Debug, PartialEq, Eq)]
enum Command {
    Devices {
        json: bool,
    },
    InspectIpa {
        ipa: PathBuf,
        json: bool,
    },
    SessionStatus {
        session_dir: PathBuf,
        json: bool,
    },
    Login {
        session_dir: PathBuf,
        email: String,
        overwrite: bool,
        json: bool,
    },
    InstallIpa {
        ipa: PathBuf,
        session_dir: PathBuf,
        udid: Option<String>,
        team: Option<String>,
        cert_policy: CertPolicy,
        ext_policy: ExtensionPolicy,
        json: bool,
    },
    InstallPairing {
        udid: Option<String>,
        bundle: Option<String>,
        target: PairingTarget,
        sync_session_dir: Option<PathBuf>,
        save: Option<PathBuf>,
        json: bool,
    },
    RenewUsb {
        session_dir: PathBuf,
        udid: Option<String>,
        bundle: String,
        team: Option<String>,
        json: bool,
    },
    Help,
}

const USAGE: &str = "\
Usage: dopamine-loader-core <command> [options]

Commands:
  devices [--json]
  inspect-ipa --ipa PATH [--json]
  session-status --session-dir DIR [--json]
  login --session-dir DIR --email EMAIL [--overwrite] [--json]
  install-ipa --ipa PATH --session-dir DIR [--udid UDID] [--team TEAM] [--max-certs error|revoke] [--extensions register|reuse] [--json]
  install-pairing [--udid UDID] [--bundle BUNDLE_ID] [--target auto|sidebox|sidestore] [--sync-session-dir DIR] [--save PATH] [--json]
  renew-usb --session-dir DIR --bundle BUNDLE_ID [--udid UDID] [--team TEAM] [--json]
";

fn take_opt(args: &[String], idx: &mut usize, flag: &str) -> Result<String, String> {
    if *idx + 1 >= args.len() {
        return Err(format!("флаг {flag} требует значения"));
    }
    *idx += 1;
    Ok(args[*idx].clone())
}

fn parse_cli(args: &[String]) -> Result<Command, (bool, String)> {
    let json = args.iter().any(|a| a == "--json");
    let filtered: Vec<String> = args
        .iter()
        .filter(|a| a.as_str() != "--json")
        .cloned()
        .collect();

    let Some(cmd) = filtered.first().map(String::as_str) else {
        return Err((json, USAGE.to_string()));
    };
    if cmd == "-h" || cmd == "--help" || cmd == "help" {
        return Ok(Command::Help);
    }

    let rest = &filtered[1..];
    match cmd {
        "devices" => {
            if !rest.is_empty() {
                return Err((json, format!("лишние аргументы для devices: {}", rest.join(" "))));
            }
            Ok(Command::Devices { json })
        }
        "inspect-ipa" => {
            let mut ipa: Option<PathBuf> = None;
            let mut i = 0;
            while i < rest.len() {
                match rest[i].as_str() {
                    "--ipa" => {
                        ipa = Some(PathBuf::from(
                            take_opt(rest, &mut i, "--ipa").map_err(|e| (json, e))?,
                        ));
                    }
                    other => {
                        return Err((json, format!("неизвестный аргумент для inspect-ipa: {other}")));
                    }
                }
                i += 1;
            }
            let ipa = ipa.ok_or_else(|| (json, "укажите --ipa PATH".to_string()))?;
            Ok(Command::InspectIpa { ipa, json })
        }
        "session-status" => {
            let mut session_dir: Option<PathBuf> = None;
            let mut i = 0;
            while i < rest.len() {
                match rest[i].as_str() {
                    "--session-dir" => {
                        session_dir = Some(PathBuf::from(
                            take_opt(rest, &mut i, "--session-dir").map_err(|e| (json, e))?,
                        ));
                    }
                    other => {
                        return Err((json, format!("неизвестный аргумент для session-status: {other}")));
                    }
                }
                i += 1;
            }
            let session_dir = session_dir
                .ok_or_else(|| (json, "укажите --session-dir DIR".to_string()))?;
            Ok(Command::SessionStatus { session_dir, json })
        }
        "login" => {
            let mut session_dir: Option<PathBuf> = None;
            let mut email: Option<String> = None;
            let mut overwrite = false;
            let mut i = 0;
            while i < rest.len() {
                match rest[i].as_str() {
                    "--session-dir" => {
                        session_dir = Some(PathBuf::from(
                            take_opt(rest, &mut i, "--session-dir").map_err(|e| (json, e))?,
                        ));
                    }
                    "--email" => {
                        email = Some(take_opt(rest, &mut i, "--email").map_err(|e| (json, e))?);
                    }
                    "--overwrite" => overwrite = true,
                    arg if arg == "--password"
                        || arg == "-p"
                        || arg.starts_with("--password=")
                        || arg.starts_with("-p=") =>
                    {
                        return Err((
                            json,
                            "передача пароля через аргументы CLI запрещена; передайте пароль через stdin".to_string(),
                        ));
                    }
                    other => {
                        return Err((json, format!("неизвестный аргумент для login: {other}")));
                    }
                }
                i += 1;
            }
            let session_dir = session_dir
                .ok_or_else(|| (json, "укажите --session-dir DIR".to_string()))?;
            let email = email.ok_or_else(|| (json, "укажите --email EMAIL".to_string()))?;
            Ok(Command::Login {
                session_dir,
                email,
                overwrite,
                json,
            })
        }
        "install-ipa" => {
            let mut ipa: Option<PathBuf> = None;
            let mut session_dir: Option<PathBuf> = None;
            let mut udid: Option<String> = None;
            let mut team: Option<String> = None;
            let mut cert_policy = CertPolicy::Error;
            let mut ext_policy = ExtensionPolicy::RegisterAll;
            let mut i = 0;
            while i < rest.len() {
                match rest[i].as_str() {
                    "--ipa" => {
                        ipa = Some(PathBuf::from(
                            take_opt(rest, &mut i, "--ipa").map_err(|e| (json, e))?,
                        ));
                    }
                    "--session-dir" => {
                        session_dir = Some(PathBuf::from(
                            take_opt(rest, &mut i, "--session-dir").map_err(|e| (json, e))?,
                        ));
                    }
                    "--udid" => {
                        udid = Some(take_opt(rest, &mut i, "--udid").map_err(|e| (json, e))?);
                    }
                    "--team" => {
                        team = Some(take_opt(rest, &mut i, "--team").map_err(|e| (json, e))?);
                    }
                    "--max-certs" => {
                        let val = take_opt(rest, &mut i, "--max-certs").map_err(|e| (json, e))?;
                        cert_policy = match val.as_str() {
                            "error" => CertPolicy::Error,
                            "revoke" => CertPolicy::Revoke,
                            _ => {
                                return Err((
                                    json,
                                    "значение --max-certs должно быть error или revoke".to_string(),
                                ));
                            }
                        };
                    }
                    "--extensions" => {
                        let val = take_opt(rest, &mut i, "--extensions").map_err(|e| (json, e))?;
                        ext_policy = match val.as_str() {
                            "register" => ExtensionPolicy::RegisterAll,
                            "reuse" => ExtensionPolicy::ReuseMain,
                            _ => {
                                return Err((
                                    json,
                                    "значение --extensions должно быть register или reuse".to_string(),
                                ));
                            }
                        };
                    }
                    other => {
                        return Err((json, format!("неизвестный аргумент для install-ipa: {other}")));
                    }
                }
                i += 1;
            }
            let ipa = ipa.ok_or_else(|| (json, "укажите --ipa PATH".to_string()))?;
            let session_dir = session_dir
                .ok_or_else(|| (json, "укажите --session-dir DIR".to_string()))?;
            Ok(Command::InstallIpa {
                ipa,
                session_dir,
                udid,
                team,
                cert_policy,
                ext_policy,
                json,
            })
        }
        "install-pairing" => {
            let mut udid: Option<String> = None;
            let mut bundle: Option<String> = None;
            let mut target = PairingTarget::Auto;
            let mut sync_session_dir: Option<PathBuf> = None;
            let mut save: Option<PathBuf> = None;
            let mut i = 0;
            while i < rest.len() {
                match rest[i].as_str() {
                    "--udid" => {
                        udid = Some(take_opt(rest, &mut i, "--udid").map_err(|e| (json, e))?);
                    }
                    "--bundle" => {
                        bundle = Some(take_opt(rest, &mut i, "--bundle").map_err(|e| (json, e))?);
                    }
                    "--target" => {
                        let val = take_opt(rest, &mut i, "--target").map_err(|e| (json, e))?;
                        target = match val.as_str() {
                            "auto" => PairingTarget::Auto,
                            "sidebox" => PairingTarget::SideBox,
                            "sidestore" => PairingTarget::SideStore,
                            _ => {
                                return Err((
                                    json,
                                    "значение --target должно быть auto, sidebox или sidestore".to_string(),
                                ));
                            }
                        };
                    }
                    "--sync-session-dir" => {
                        sync_session_dir = Some(PathBuf::from(
                            take_opt(rest, &mut i, "--sync-session-dir").map_err(|e| (json, e))?,
                        ));
                    }
                    "--save" => {
                        save = Some(PathBuf::from(
                            take_opt(rest, &mut i, "--save").map_err(|e| (json, e))?,
                        ));
                    }
                    other => {
                        return Err((
                            json,
                            format!("неизвестный аргумент для install-pairing: {other}"),
                        ));
                    }
                }
                i += 1;
            }
            Ok(Command::InstallPairing {
                udid,
                bundle,
                target,
                sync_session_dir,
                save,
                json,
            })
        }
        "renew-usb" => {
            let mut session_dir: Option<PathBuf> = None;
            let mut udid: Option<String> = None;
            let mut bundle: Option<String> = None;
            let mut team: Option<String> = None;
            let mut i = 0;
            while i < rest.len() {
                match rest[i].as_str() {
                    "--session-dir" => {
                        session_dir = Some(PathBuf::from(
                            take_opt(rest, &mut i, "--session-dir").map_err(|e| (json, e))?,
                        ));
                    }
                    "--udid" => {
                        udid = Some(take_opt(rest, &mut i, "--udid").map_err(|e| (json, e))?);
                    }
                    "--bundle" => {
                        bundle = Some(take_opt(rest, &mut i, "--bundle").map_err(|e| (json, e))?);
                    }
                    "--team" => {
                        team = Some(take_opt(rest, &mut i, "--team").map_err(|e| (json, e))?);
                    }
                    other => {
                        return Err((json, format!("неизвестный аргумент для renew-usb: {other}")));
                    }
                }
                i += 1;
            }
            let session_dir = session_dir
                .ok_or_else(|| (json, "укажите --session-dir DIR".to_string()))?;
            let bundle = bundle.ok_or_else(|| (json, "укажите --bundle BUNDLE_ID".to_string()))?;
            Ok(Command::RenewUsb {
                session_dir,
                udid,
                bundle,
                team,
                json,
            })
        }
        other => Err((json, format!("неизвестная команда: {other}\n{USAGE}"))),
    }
}

fn init_crypto_and_isideload() -> Result<(), String> {
    let _ = rustls::crypto::ring::default_provider().install_default();
    isideload::init()
        .map_err(|_| "не удалось инициализировать библиотеку подписи isideload".to_string())
}

#[tokio::main]
async fn main() -> ExitCode {
    let args: Vec<String> = env::args().skip(1).collect();
    let cmd = match parse_cli(&args) {
        Ok(c) => c,
        Err((json, msg)) => {
            if json {
                let emitter = Emitter::new(true);
                emitter.error("cli", msg);
            } else {
                eprintln!("{msg}");
            }
            return ExitCode::FAILURE;
        }
    };

    match cmd {
        Command::Help => {
            print!("{USAGE}");
            ExitCode::SUCCESS
        }
        Command::InspectIpa { ipa, json } => {
            let resp = usb::inspect_ipa(&ipa);
            if json {
                print_json_value(&resp);
            } else if resp.ok {
                println!("{}", resp.message);
            } else {
                eprintln!("{}", resp.message);
            }
            if resp.ok {
                ExitCode::SUCCESS
            } else {
                ExitCode::FAILURE
            }
        }
        Command::SessionStatus { session_dir, json } => {
            let st = session::inspect_session(&session_dir);
            if json {
                print_json_value(&st);
            } else if st.valid {
                println!("{}", st.message);
            } else {
                eprintln!("{}", st.message);
            }
            if st.valid {
                ExitCode::SUCCESS
            } else {
                ExitCode::FAILURE
            }
        }
        Command::Devices { json } => {
            let _ = rustls::crypto::ring::default_provider().install_default();
            let resp = usb::list_devices().await;
            if json {
                print_json_value(&resp);
            } else if !resp.ok {
                eprintln!("{}", resp.message);
            } else if resp.devices.is_empty() {
                println!("{}", resp.message);
            } else {
                for d in &resp.devices {
                    println!(
                        "{} (iOS {}, {}, udid={}, paired={})",
                        d.name,
                        if d.product_version.is_empty() {
                            "?"
                        } else {
                            &d.product_version
                        },
                        d.connection_type,
                        d.udid,
                        d.paired
                    );
                }
            }
            if resp.ok {
                ExitCode::SUCCESS
            } else {
                ExitCode::FAILURE
            }
        }
        Command::Login {
            session_dir,
            email,
            overwrite,
            json,
        } => {
            let emitter = Emitter::new(json);
            if let Err(err) = init_crypto_and_isideload() {
                emitter.error("authenticating", err);
                return ExitCode::FAILURE;
            }
            match session::login_apple_id(&session_dir, &email, overwrite, &emitter).await {
                Ok(masked) => {
                    emitter.emit_event(&StreamEvent::Done {
                        ok: true,
                        email_masked: Some(masked.clone()),
                        bundle_id: None,
                        app_name: None,
                        udid: None,
                        special_app: None,
                        team_id: None,
                        expires_at_unix: None,
                        hours_remaining: None,
                        saved_path: None,
                        remote_path: None,
                        synced_session: None,
                        message: format!("Вход в Apple ID ({masked}) выполнен, сессия сохранена."),
                    });
                    ExitCode::SUCCESS
                }
                Err(err) => {
                    emitter.error("authenticating", err);
                    ExitCode::FAILURE
                }
            }
        }
        Command::InstallIpa {
            ipa,
            session_dir,
            udid,
            team,
            cert_policy,
            ext_policy,
            json,
        } => {
            let emitter = Emitter::new(json);
            if let Err(err) = init_crypto_and_isideload() {
                emitter.error("connecting", err);
                return ExitCode::FAILURE;
            }
            match usb::install_ipa(
                &ipa,
                &session_dir,
                udid.as_deref(),
                team.as_deref(),
                cert_policy,
                ext_policy,
                &emitter,
            )
            .await
            {
                Ok(done) => {
                    emitter.emit_event(&done);
                    ExitCode::SUCCESS
                }
                Err((stage, msg)) => {
                    emitter.error(&stage, msg);
                    ExitCode::FAILURE
                }
            }
        }
        Command::InstallPairing {
            udid,
            bundle,
            target,
            sync_session_dir,
            save,
            json,
        } => {
            let _ = rustls::crypto::ring::default_provider().install_default();
            let emitter = Emitter::new(json);
            match usb::install_pairing(
                udid.as_deref(),
                bundle.as_deref(),
                target,
                sync_session_dir.as_deref(),
                save.as_deref(),
                &emitter,
            )
            .await
            {
                Ok(done) => {
                    emitter.emit_event(&done);
                    ExitCode::SUCCESS
                }
                Err((stage, msg)) => {
                    emitter.error(&stage, msg);
                    ExitCode::FAILURE
                }
            }
        }
        Command::RenewUsb {
            session_dir,
            udid,
            bundle,
            team,
            json,
        } => {
            let emitter = Emitter::new(json);
            if let Err(err) = init_crypto_and_isideload() {
                emitter.error("connecting", err);
                return ExitCode::FAILURE;
            }
            match usb::renew_usb(
                &session_dir,
                udid.as_deref(),
                &bundle,
                team.as_deref(),
                &emitter,
            )
            .await
            {
                Ok(done) => {
                    emitter.emit_event(&done);
                    ExitCode::SUCCESS
                }
                Err((stage, msg)) => {
                    emitter.error(&stage, msg);
                    ExitCode::FAILURE
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn s(items: &[&str]) -> Vec<String> {
        items.iter().map(|x| (*x).to_string()).collect()
    }

    #[test]
    fn cli_parses_all_subcommands_and_rejects_password_arg() {
        assert_eq!(
            parse_cli(&s(&["--json", "devices"])).unwrap(),
            Command::Devices { json: true }
        );
        assert_eq!(
            parse_cli(&s(&["session-status", "--session-dir", "/tmp/s", "--json"])).unwrap(),
            Command::SessionStatus {
                session_dir: PathBuf::from("/tmp/s"),
                json: true
            }
        );
        assert_eq!(
            parse_cli(&s(&[
                "login",
                "--session-dir",
                "/tmp/s",
                "--email",
                "a@b.com",
                "--overwrite",
                "--json"
            ]))
            .unwrap(),
            Command::Login {
                session_dir: PathBuf::from("/tmp/s"),
                email: "a@b.com".into(),
                overwrite: true,
                json: true
            }
        );
        // Must explicitly reject passing password on CLI argv without echoing it!
        assert!(
            parse_cli(&s(&[
                "login",
                "--session-dir",
                "/tmp/s",
                "--email",
                "a@b.com",
                "--password",
                "secret"
            ]))
            .is_err()
        );
        let eq_err = parse_cli(&s(&[
            "login",
            "--session-dir",
            "/tmp/s",
            "--email",
            "a@b.com",
            "--password=TOP_SECRET_VALUE",
        ]))
        .unwrap_err();
        assert!(!eq_err.1.contains("TOP_SECRET_VALUE"));
        let short_eq_err = parse_cli(&s(&[
            "login",
            "--session-dir",
            "/tmp/s",
            "--email",
            "a@b.com",
            "-p=SHORT_SECRET_VAL",
        ]))
        .unwrap_err();
        assert!(!short_eq_err.1.contains("SHORT_SECRET_VAL"));
        assert_eq!(
            parse_cli(&s(&[
                "install-ipa",
                "--ipa",
                "/tmp/a.ipa",
                "--session-dir",
                "/tmp/s",
                "--max-certs",
                "revoke",
                "--extensions",
                "reuse",
                "--json"
            ]))
            .unwrap(),
            Command::InstallIpa {
                ipa: PathBuf::from("/tmp/a.ipa"),
                session_dir: PathBuf::from("/tmp/s"),
                udid: None,
                team: None,
                cert_policy: CertPolicy::Revoke,
                ext_policy: ExtensionPolicy::ReuseMain,
                json: true
            }
        );
        assert_eq!(
            parse_cli(&s(&["inspect-ipa", "--ipa", "/tmp/SideBox.ipa", "--json"])).unwrap(),
            Command::InspectIpa {
                ipa: PathBuf::from("/tmp/SideBox.ipa"),
                json: true
            }
        );
        assert_eq!(
            parse_cli(&s(&[
                "install-pairing",
                "--target",
                "sidebox",
                "--sync-session-dir",
                "/tmp/sess",
                "--json"
            ]))
            .unwrap(),
            Command::InstallPairing {
                udid: None,
                bundle: None,
                target: PairingTarget::SideBox,
                sync_session_dir: Some(PathBuf::from("/tmp/sess")),
                save: None,
                json: true
            }
        );
        assert_eq!(
            parse_cli(&s(&["install-pairing", "--json"])).unwrap(),
            Command::InstallPairing {
                udid: None,
                bundle: None,
                target: PairingTarget::Auto,
                sync_session_dir: None,
                save: None,
                json: true
            }
        );
        assert_eq!(
            parse_cli(&s(&[
                "renew-usb",
                "--session-dir",
                "/tmp/s",
                "--bundle",
                "com.SideStore.SideStore.TEAM123456",
                "--json"
            ]))
            .unwrap(),
            Command::RenewUsb {
                session_dir: PathBuf::from("/tmp/s"),
                udid: None,
                bundle: "com.SideStore.SideStore.TEAM123456".into(),
                team: None,
                json: true
            }
        );
    }
}
