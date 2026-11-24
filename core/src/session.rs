use crate::protocol::{Emitter, SessionStatusResponse, mask_email, sanitize_message};
use isideload::{
    anisette::remote_v3::RemoteV3AnisetteProvider,
    auth::apple_account::{AppleAccount, TwoFactorCallbackParams, TwoFactorCallbackResponse},
    util::storage::SideloadingStorage,
};
use rootcause::prelude::*;
use std::{
    fs,
    io::{self, IsTerminal, Write},
    os::unix::fs::{DirBuilderExt, OpenOptionsExt, PermissionsExt},
    path::{Component, Path, PathBuf},
    sync::{
        Arc,
        atomic::{AtomicU64, AtomicUsize, Ordering},
    },
    time::{SystemTime, UNIX_EPOCH},
};
use tokio::io::{AsyncBufReadExt, BufReader};
use zeroize::Zeroize;

pub const ANISETTE_SERVER: &str = "https://mac-ani.jkcoxson.com";

static TMP_SEQ: AtomicU64 = AtomicU64::new(0);

pub struct PrivateFsStorage {
    root: PathBuf,
}

impl PrivateFsStorage {
    pub fn new(root: PathBuf) -> Self {
        Self { root }
    }

    fn safe_join(&self, key: &str) -> Result<PathBuf, Report> {
        let rel = Path::new(key);
        if key.is_empty() || rel.is_absolute() {
            bail!("Invalid storage key");
        }
        let mut out = self.root.clone();
        for comp in rel.components() {
            match comp {
                Component::Normal(part) => out.push(part),
                Component::CurDir => {}
                _ => bail!("Disallowed path component in storage key"),
            }
        }
        Ok(out)
    }
}

impl SideloadingStorage for PrivateFsStorage {
    fn store_data(&self, key: &str, data: &[u8]) -> Result<(), Report> {
        ensure_private_dir(&self.root, true).map_err(|e| report!("{e}"))?;
        let path = self.safe_join(key)?;
        if let Some(parent) = path.parent() {
            if parent != self.root {
                ensure_private_dir_recursive(&self.root, parent).map_err(|e| report!("{e}"))?;
            }
        }
        write_private_atomic(&path, data, true).map_err(|e| report!("{e}"))?;
        Ok(())
    }

    fn retrieve_data(&self, key: &str) -> Result<Option<Vec<u8>>, Report> {
        let path = self.safe_join(key)?;
        match fs::symlink_metadata(&path) {
            Ok(meta) => {
                if meta.file_type().is_symlink() || !meta.is_file() {
                    bail!("Storage entry must be a regular file");
                }
            }
            Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(None),
            Err(e) => return Err(report!(e).into()),
        }
        let bytes = fs::read(&path).context("Failed to read storage file")?;
        Ok(Some(bytes))
    }

    fn store(&self, key: &str, value: &str) -> Result<(), Report> {
        self.store_data(key, value.as_bytes())
    }

    fn retrieve(&self, key: &str) -> Result<Option<String>, Report> {
        match self.retrieve_data(key)? {
            Some(bytes) => Ok(Some(String::from_utf8_lossy(&bytes).into_owned())),
            None => Ok(None),
        }
    }
}

fn ensure_private_dir_recursive(root: &Path, target: &Path) -> Result<(), String> {
    let rel = target
        .strip_prefix(root)
        .map_err(|_| "путь вне приватного каталога".to_string())?;
    let mut cur = root.to_path_buf();
    for comp in rel.components() {
        let Component::Normal(part) = comp else {
            return Err("недопустимый путь".into());
        };
        cur.push(part);
        ensure_private_dir(&cur, true)?;
    }
    Ok(())
}

pub fn ensure_private_dir(dir: &Path, create_if_missing: bool) -> Result<(), String> {
    match fs::symlink_metadata(dir) {
        Ok(meta) => {
            if meta.file_type().is_symlink() || !meta.is_dir() {
                return Err("каталог сессии должен быть обычной директорией (не symlink)".into());
            }
            if meta.permissions().mode() & 0o077 != 0 {
                return Err("каталог сессии должен иметь приватные права 0700".into());
            }
            Ok(())
        }
        Err(e) if e.kind() == io::ErrorKind::NotFound && create_if_missing => {
            match fs::DirBuilder::new().mode(0o700).create(dir) {
                Ok(()) => {}
                Err(err) if err.kind() == io::ErrorKind::AlreadyExists => {}
                Err(_) => return Err("не удалось создать приватный каталог сессии".into()),
            }
            let meta = fs::symlink_metadata(dir)
                .map_err(|_| "не удалось проверить каталог сессии".to_string())?;
            if meta.file_type().is_symlink()
                || !meta.is_dir()
                || meta.permissions().mode() & 0o077 != 0
            {
                return Err("каталог сессии должен иметь приватные права 0700".into());
            }
            Ok(())
        }
        Err(_) => Err("каталог сессии недоступен".into()),
    }
}

pub fn write_private_atomic(path: &Path, data: &[u8], overwrite: bool) -> Result<(), String> {
    let parent = path
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .unwrap_or_else(|| Path::new("."));
    if let Ok(meta) = fs::symlink_metadata(path) {
        if meta.file_type().is_symlink() || !meta.is_file() {
            return Err("целевой путь не является обычным файлом".into());
        }
        if !overwrite {
            return Err("файл уже существует (укажите --overwrite для перезаписи)".into());
        }
    }
    let pid = std::process::id();
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    let seq = TMP_SEQ.fetch_add(1, Ordering::Relaxed);
    let file_name = path
        .file_name()
        .and_then(|n| n.to_str())
        .unwrap_or("private");
    let tmp_path = parent.join(format!(".{file_name}.tmp.{pid}-{nanos}-{seq}"));
    let write_res = (|| -> io::Result<()> {
        let mut f = fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&tmp_path)?;
        f.write_all(data)?;
        f.sync_all()?;
        fs::rename(&tmp_path, path)?;
        Ok(())
    })();
    if let Err(err) = write_res {
        let _ = fs::remove_file(&tmp_path);
        return Err(format!("не удалось безопасно сохранить файл: {err}"));
    }
    Ok(())
}

pub fn inspect_session(dir: &Path) -> SessionStatusResponse {
    let dir_meta = match fs::symlink_metadata(dir) {
        Ok(m) => m,
        Err(e) if e.kind() == io::ErrorKind::NotFound => {
            return SessionStatusResponse {
                ok: false,
                exists: false,
                valid: false,
                dir_exists: false,
                dir_private: false,
                session_private: false,
                anisette_state_exists: false,
                email_masked: None,
                message: "Сессия Apple ID не найдена (каталог не создан).".to_string(),
                error: Some("Каталог сессии не существует.".to_string()),
            };
        }
        Err(_) => {
            return SessionStatusResponse {
                ok: false,
                exists: false,
                valid: false,
                dir_exists: false,
                dir_private: false,
                session_private: false,
                anisette_state_exists: false,
                email_masked: None,
                message: "Не удалось проверить каталог сессии Apple ID.".to_string(),
                error: Some("Не удалось прочитать метаданные каталога сессии.".to_string()),
            };
        }
    };

    if dir_meta.file_type().is_symlink() || !dir_meta.is_dir() {
        return SessionStatusResponse {
            ok: false,
            exists: false,
            valid: false,
            dir_exists: true,
            dir_private: false,
            session_private: false,
            anisette_state_exists: false,
            email_masked: None,
            message: "Путь сессии Apple ID не является обычным каталогом.".to_string(),
            error: Some("Каталог сессии не должен быть символической ссылкой.".to_string()),
        };
    }

    let dir_private = dir_meta.permissions().mode() & 0o077 == 0;
    let anisette_state_exists = fs::symlink_metadata(dir.join("anisette_state"))
        .is_ok_and(|m| !m.file_type().is_symlink() && m.is_file());
    let session_path = dir.join("apple-session.plist");
    let session_meta = match fs::symlink_metadata(&session_path) {
        Ok(m) => m,
        Err(e) if e.kind() == io::ErrorKind::NotFound => {
            let err = if dir_private {
                "Файл apple-session.plist не найден — требуется вход в Apple ID."
            } else {
                "Каталог сессии должен иметь права 0700, и apple-session.plist отсутствует."
            };
            return SessionStatusResponse {
                ok: false,
                exists: false,
                valid: false,
                dir_exists: true,
                dir_private,
                session_private: false,
                anisette_state_exists,
                email_masked: None,
                message: err.to_string(),
                error: Some(err.to_string()),
            };
        }
        Err(_) => {
            return SessionStatusResponse {
                ok: false,
                exists: false,
                valid: false,
                dir_exists: true,
                dir_private,
                session_private: false,
                anisette_state_exists,
                email_masked: None,
                message: "Не удалось проверить apple-session.plist.".to_string(),
                error: Some("Ошибка доступа к apple-session.plist.".to_string()),
            };
        }
    };

    let session_regular = !session_meta.file_type().is_symlink() && session_meta.is_file();
    let session_private = session_regular && session_meta.permissions().mode() & 0o077 == 0;
    if !dir_private {
        let err = "Каталог сессии должен иметь права 0700.";
        return SessionStatusResponse {
            ok: false,
            exists: true,
            valid: false,
            dir_exists: true,
            dir_private: false,
            session_private,
            anisette_state_exists,
            email_masked: None,
            message: err.to_string(),
            error: Some(err.to_string()),
        };
    }
    if !session_regular {
        let err = "apple-session.plist должен быть обычным файлом.";
        return SessionStatusResponse {
            ok: false,
            exists: true,
            valid: false,
            dir_exists: true,
            dir_private: true,
            session_private: false,
            anisette_state_exists,
            email_masked: None,
            message: err.to_string(),
            error: Some(err.to_string()),
        };
    }
    if !session_private {
        let err = "apple-session.plist должен иметь приватные права 0600.";
        return SessionStatusResponse {
            ok: false,
            exists: true,
            valid: false,
            dir_exists: true,
            dir_private: true,
            session_private: false,
            anisette_state_exists,
            email_masked: None,
            message: err.to_string(),
            error: Some(err.to_string()),
        };
    }

    match plist::from_file::<_, (String, plist::Dictionary)>(&session_path) {
        Ok((email, spd)) if !email.trim().is_empty() && !spd.is_empty() => {
            let masked = mask_email(&email);
            drop(email);
            drop(spd);
            SessionStatusResponse {
                ok: true,
                exists: true,
                valid: true,
                dir_exists: true,
                dir_private: true,
                session_private: true,
                anisette_state_exists,
                email_masked: Some(masked.clone()),
                message: format!("Сессия Apple ID активна ({masked})"),
                error: None,
            }
        }
        _ => {
            let err = "Файл apple-session.plist повреждён или неполон; выполните вход заново.";
            SessionStatusResponse {
                ok: false,
                exists: true,
                valid: false,
                dir_exists: true,
                dir_private: true,
                session_private: true,
                anisette_state_exists,
                email_masked: None,
                message: err.to_string(),
                error: Some(err.to_string()),
            }
        }
    }
}

pub async fn build_apple_account(dir: &Path, email: &str) -> Result<AppleAccount, String> {
    let provider = RemoteV3AnisetteProvider::default()
        .map_err(|_| "не удалось инициализировать Anisette".to_string())?
        .set_url(ANISETTE_SERVER)
        .set_storage(Box::new(PrivateFsStorage::new(dir.to_path_buf())));
    AppleAccount::builder(email)
        .anisette_provider(provider)
        .build()
        .await
        .map_err(|e| format!("не удалось подключиться к Apple ID: {}", sanitize_message(&e.to_string())))
}

pub async fn load_saved_account(dir: &Path) -> Result<(AppleAccount, String), String> {
    ensure_private_dir(dir, false)?;
    let session_path = dir.join("apple-session.plist");
    let meta = fs::symlink_metadata(&session_path)
        .map_err(|_| "сессия Apple ID не найдена — сначала выполните вход".to_string())?;
    if meta.file_type().is_symlink() || !meta.is_file() || meta.permissions().mode() & 0o077 != 0 {
        return Err("apple-session.plist должен быть обычным файлом с правами 0600".into());
    }
    let (email, spd): (String, plist::Dictionary) = plist::from_file(&session_path)
        .map_err(|_| "не удалось прочитать сохранённую сессию Apple ID".to_string())?;
    if email.trim().is_empty() || spd.is_empty() {
        return Err("сохранённая сессия Apple ID неполна; выполните вход заново".into());
    }
    let mut account = build_apple_account(dir, &email).await?;
    account.spd = Some(spd);
    Ok((account, email))
}

async fn read_secret_line(
    stdin_reader: &Arc<tokio::sync::Mutex<BufReader<tokio::io::Stdin>>>,
    use_tty_prompt: bool,
    tty_prompt: &str,
) -> Result<String, String> {
    if use_tty_prompt {
        let prompt = tty_prompt.to_string();
        return tokio::task::spawn_blocking(move || rpassword::prompt_password(&prompt))
            .await
            .map_err(|_| "внутренняя ошибка чтения ввода".to_string())?
            .map_err(|_| "не удалось прочитать ввод с терминала".to_string());
    }
    let mut line = String::new();
    let mut guard = stdin_reader.lock().await;
    let n = guard
        .read_line(&mut line)
        .await
        .map_err(|_| "не удалось прочитать данные из stdin".to_string())?;
    if n == 0 {
        return Ok(String::new());
    }
    while line.ends_with('\n') || line.ends_with('\r') {
        line.pop();
    }
    Ok(line)
}

pub async fn login_apple_id(
    dir: &Path,
    email: &str,
    overwrite: bool,
    emitter: &Emitter,
) -> Result<String, String> {
    let email = email.trim();
    if email.is_empty() || !email.contains('@') {
        return Err("укажите корректный адрес Apple ID".into());
    }
    ensure_private_dir(dir, true)?;
    let session_path = dir.join("apple-session.plist");
    if let Ok(meta) = fs::symlink_metadata(&session_path) {
        if meta.file_type().is_symlink() || !meta.is_file() {
            return Err("apple-session.plist не является обычным файлом".into());
        }
        if !overwrite {
            return Err(
                "сессия Apple ID уже существует (передайте --overwrite для повторного входа)".into(),
            );
        }
    }

    let use_tty_prompt = !emitter.is_json() && io::stdin().is_terminal();
    let stdin_reader = Arc::new(tokio::sync::Mutex::new(BufReader::new(tokio::io::stdin())));

    let mut password = read_secret_line(&stdin_reader, use_tty_prompt, "Пароль Apple ID: ").await?;
    if password.is_empty() {
        return Err("пароль Apple ID не передан".into());
    }

    emitter.progress("authenticating", 15, "Подключение к серверам Apple...");
    let account_res = build_apple_account(dir, email).await;
    let mut account = match account_res {
        Ok(acc) => acc,
        Err(err) => {
            password.zeroize();
            return Err(err);
        }
    };

    emitter.progress("authenticating", 35, "Проверка учётных данных Apple ID...");
    let unknown_attempts = Arc::new(AtomicUsize::new(0));
    let reader_clone = Arc::clone(&stdin_reader);
    let emitter_ref = emitter;

    let login_res = account
        .login(&password, move |params: TwoFactorCallbackParams| {
            let reader = Arc::clone(&reader_clone);
            let unknown_count = Arc::clone(&unknown_attempts);
            async move {
                if params.unknown {
                    let tries = unknown_count.fetch_add(1, Ordering::Relaxed);
                    if tries == 0 {
                        return Ok(TwoFactorCallbackResponse::SendToDevices);
                    }
                    if let Some(num) = params.numbers.first() {
                        return Ok(TwoFactorCallbackResponse::SendSms(num.id));
                    }
                    return Ok(TwoFactorCallbackResponse::Abort);
                }

                let retry = params.last_error.is_some();
                let msg = if retry {
                    "Неверный код подтверждения. Введите 6-значный код Apple ID ещё раз:"
                } else if params.sms {
                    "Введите 6-значный код подтверждения Apple ID из SMS:"
                } else {
                    "Введите 6-значный код подтверждения Apple ID (отправлен на ваши устройства):"
                };
                emitter_ref.need_2fa(params.sms, retry, params.last_error, msg);

                let mut code = read_secret_line(
                    &reader,
                    use_tty_prompt,
                    "Код подтверждения (пусто — отмена): ",
                )
                .await
                .unwrap_or_default();
                let trimmed = code.trim().to_string();
                code.zeroize();

                if trimmed.is_empty() {
                    Ok(TwoFactorCallbackResponse::Abort)
                } else if trimmed.eq_ignore_ascii_case("resend") {
                    Ok(TwoFactorCallbackResponse::ResendCode)
                } else {
                    Ok(TwoFactorCallbackResponse::SubmitCode(trimmed))
                }
            }
        })
        .await;

    password.zeroize();
    if let Err(err) = login_res {
        return Err(format!(
            "не удалось войти в Apple ID: {}",
            sanitize_message(&err.to_string())
        ));
    }

    emitter.progress("authenticating", 85, "Сохранение приватной сессии Apple ID...");
    let spd = account
        .spd
        .as_ref()
        .ok_or("Apple не вернула данные сессии (SPD)")?;
    let mut bytes = Vec::new();
    let record = (email, spd);
    plist::to_writer_binary(&mut bytes, &record)
        .map_err(|_| "не удалось сериализовать сессию Apple ID".to_string())?;
    let save_res = write_private_atomic(&session_path, &bytes, overwrite);
    bytes.zeroize();
    save_res?;

    Ok(mask_email(email))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::symlink;

    fn temp_dir(label: &str) -> PathBuf {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let seq = TMP_SEQ.fetch_add(1, Ordering::Relaxed);
        std::env::temp_dir().join(format!(
            "dopamine-loader-session-{label}-{}-{nanos}-{seq}",
            std::process::id()
        ))
    }

    fn write_session(path: &Path, email: &str, token: &str, mode: u32) {
        let mut spd = plist::Dictionary::new();
        spd.insert("GsIdmsToken".into(), plist::Value::String(token.into()));
        let mut bytes = Vec::new();
        plist::to_writer_binary(&mut bytes, &(email, spd)).unwrap();
        let mut f = fs::OpenOptions::new()
            .create(true)
            .truncate(true)
            .write(true)
            .mode(mode)
            .open(path)
            .unwrap();
        f.write_all(&bytes).unwrap();
        fs::set_permissions(path, fs::Permissions::from_mode(mode)).unwrap();
    }

    #[test]
    fn inspect_session_enforces_permissions_and_masks_email_without_leaking_secrets() {
        let dir = temp_dir("valid");
        fs::DirBuilder::new().mode(0o700).create(&dir).unwrap();
        fs::set_permissions(&dir, fs::Permissions::from_mode(0o700)).unwrap();

        let session = dir.join("apple-session.plist");
        write_session(&session, "alice.secret@icloud.com", "TOP_SECRET_SPD_123", 0o600);
        fs::write(dir.join("anisette_state"), b"state").unwrap();

        let st = inspect_session(&dir);
        assert!(st.ok);
        assert!(st.valid);
        assert!(st.exists);
        assert!(st.dir_private);
        assert!(st.session_private);
        assert!(st.anisette_state_exists);
        assert_eq!(st.email_masked.as_deref(), Some("a***@icloud.com"));

        let json = serde_json::to_string(&st).unwrap();
        assert!(!json.contains("TOP_SECRET_SPD_123"));
        assert!(!json.contains("alice.secret"));

        // Insecure 0644 file must be rejected
        fs::set_permissions(&session, fs::Permissions::from_mode(0o644)).unwrap();
        let bad_file = inspect_session(&dir);
        assert!(!bad_file.valid);
        assert!(!bad_file.session_private);

        // Insecure 0755 dir must be rejected
        fs::set_permissions(&session, fs::Permissions::from_mode(0o600)).unwrap();
        fs::set_permissions(&dir, fs::Permissions::from_mode(0o755)).unwrap();
        let bad_dir = inspect_session(&dir);
        assert!(!bad_dir.valid);
        assert!(!bad_dir.dir_private);

        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn private_fs_storage_enforces_0700_0600_and_blocks_traversal() {
        let dir = temp_dir("storage");
        let storage = PrivateFsStorage::new(dir.clone());
        assert!(storage.store_data("../escape", b"bad").is_err());
        assert!(storage.store_data("/etc/passwd", b"bad").is_err());

        storage.store_data("sub/key", b"private-key-bytes").unwrap();
        let dir_mode = fs::metadata(&dir).unwrap().permissions().mode() & 0o777;
        let sub_mode = fs::metadata(dir.join("sub")).unwrap().permissions().mode() & 0o777;
        let key_mode = fs::metadata(dir.join("sub/key")).unwrap().permissions().mode() & 0o777;
        assert_eq!(dir_mode, 0o700);
        assert_eq!(sub_mode, 0o700);
        assert_eq!(key_mode, 0o600);
        assert_eq!(
            storage.retrieve_data("sub/key").unwrap().as_deref(),
            Some(&b"private-key-bytes"[..])
        );

        // Symlink entry must be rejected on read/write
        let link = dir.join("link");
        symlink(dir.join("sub/key"), &link).unwrap();
        assert!(storage.retrieve_data("link").is_err());
        assert!(storage.store_data("link", b"x").is_err());

        let _ = fs::remove_dir_all(&dir);
    }
}
