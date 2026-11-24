use crate::{
    profile::{
        check_replacement_compatible, find_newest_matching_profile, parse_cms_profile_dict,
        profile_team_id, validate_bundle_id,
    },
    protocol::{
        DeviceEntry, DevicesResponse, Emitter, InspectIpaResponse, StreamEvent, sanitize_message,
    },
    session::{
        PrivateFsStorage, ensure_private_dir, load_saved_account, write_private_atomic,
    },
};
use idevice::{
    IdeviceService,
    afc::opcode::AfcFopenMode,
    house_arrest::HouseArrestClient,
    installation_proxy::InstallationProxyClient,
    lockdown::LockdownClient,
    misagent::MisagentClient,
    provider::IdeviceProvider,
    usbmuxd::{Connection, UsbmuxdAddr, UsbmuxdDevice},
};
use isideload::{
    dev::{
        developer_session::{AppIdsApi, DeveloperDeviceType, DeveloperSession, TeamsApi},
        devices::DevicesApi,
    },
    sideload::{
        SideloaderBuilder, TeamSelection,
        application::Application,
        builder::{ExtensionsBehavior, ExtensionsBehaviorSelection, MaxCertsBehavior},
    },
    util::{
        callbacks::{ExtensionsBehaviorCallbackBox, MaxCertsCallbackBox},
        device::IdeviceInfo,
    },
};
use std::{
    collections::HashMap,
    fs,
    path::{Path, PathBuf},
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use zeroize::Zeroize;

const PAIRING_REMOTE_PATH_SIDESTORE: &str = "/Documents/ALTPairingFile.mobiledevicepairing";
const PAIRING_REMOTE_PATH_SIDEBOX: &str = "/Documents/pairingFile.plist";
const SESSION_REMOTE_PATH: &str = "/Documents/apple-session.plist";
const ANISETTE_REMOTE_PATH: &str = "/Documents/anisette_state";
const MAX_SYNC_FILE_BYTES: u64 = 1024 * 1024;
const LOCKDOWN_TIMEOUT: Duration = Duration::from_secs(5);
const SERVICE_TIMEOUT: Duration = Duration::from_secs(20);

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PairingTarget {
    Auto,
    SideBox,
    SideStore,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CertPolicy {
    Error,
    Revoke,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ExtensionPolicy {
    RegisterAll,
    ReuseMain,
}

fn now_unix() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

fn usbmuxd_addr() -> UsbmuxdAddr {
    UsbmuxdAddr::from_env_var().unwrap_or_default()
}

fn connection_label(conn: &Connection) -> &'static str {
    match conn {
        Connection::Usb => "usb",
        Connection::Network(_) => "network",
        Connection::Unknown(_) => "unknown",
    }
}

fn dedupe_devices_prefer_usb(devices: Vec<UsbmuxdDevice>) -> Vec<UsbmuxdDevice> {
    let mut order: Vec<String> = Vec::new();
    let mut by_udid: HashMap<String, UsbmuxdDevice> = HashMap::new();
    for dev in devices {
        let key = dev.udid.to_ascii_lowercase();
        match by_udid.get(&key) {
            None => {
                order.push(key.clone());
                by_udid.insert(key, dev);
            }
            Some(existing) => {
                if dev.connection_type == Connection::Usb
                    && existing.connection_type != Connection::Usb
                {
                    by_udid.insert(key, dev);
                }
            }
        }
    }
    let mut result: Vec<UsbmuxdDevice> = order
        .into_iter()
        .filter_map(|k| by_udid.remove(&k))
        .collect();
    result.sort_by_key(|d| (d.connection_type != Connection::Usb, d.device_id));
    result
}

async fn probe_device_info(addr: &UsbmuxdAddr, dev: &UsbmuxdDevice) -> DeviceEntry {
    let provider = dev.to_provider(addr.clone(), "DopamineLoader");
    let mut name = String::new();
    let mut product_type = String::new();
    let mut product_version = String::new();
    let mut paired = false;

    let pair_record = tokio::time::timeout(LOCKDOWN_TIMEOUT, provider.get_pairing_file())
        .await
        .ok()
        .and_then(Result::ok);

    if let Ok(Ok(mut lockdown)) =
        tokio::time::timeout(LOCKDOWN_TIMEOUT, LockdownClient::connect(&provider)).await
    {
        if let Some(ref pairing) = pair_record {
            if tokio::time::timeout(LOCKDOWN_TIMEOUT, lockdown.start_session(pairing))
                .await
                .is_ok_and(|r| r.is_ok())
            {
                paired = true;
            }
        }

        if let Ok(Ok(val)) =
            tokio::time::timeout(LOCKDOWN_TIMEOUT, lockdown.get_value(Some("DeviceName"), None))
                .await
        {
            if let Some(s) = val.as_string() {
                name = s.to_string();
            }
        }
        if let Ok(Ok(val)) =
            tokio::time::timeout(LOCKDOWN_TIMEOUT, lockdown.get_value(Some("ProductType"), None))
                .await
        {
            if let Some(s) = val.as_string() {
                product_type = s.to_string();
            }
        }
        if let Ok(Ok(val)) = tokio::time::timeout(
            LOCKDOWN_TIMEOUT,
            lockdown.get_value(Some("ProductVersion"), None),
        )
        .await
        {
            if let Some(s) = val.as_string() {
                product_version = s.to_string();
            }
        }
    }

    if name.is_empty() {
        name = if !product_type.is_empty() {
            product_type.clone()
        } else {
            "iPhone".to_string()
        };
    }

    DeviceEntry {
        udid: dev.udid.clone(),
        device_id: dev.device_id,
        connection_type: connection_label(&dev.connection_type).to_string(),
        name,
        product_type,
        product_version,
        paired,
    }
}

pub async fn list_devices() -> DevicesResponse {
    let addr = usbmuxd_addr();
    let mut conn = match tokio::time::timeout(LOCKDOWN_TIMEOUT, addr.connect(0)).await {
        Ok(Ok(c)) => c,
        Ok(Err(e)) => {
            let msg = format!(
                "Служба usbmuxd не запущена или недоступна ({}). Запустите usbmuxd и подключите iPhone по USB.",
                sanitize_message(&e.to_string())
            );
            return DevicesResponse {
                ok: false,
                usbmuxd_available: false,
                devices: Vec::new(),
                message: msg.clone(),
                error: Some(msg),
            };
        }
        Err(_) => {
            let msg = "Таймаут подключения к службе usbmuxd.".to_string();
            return DevicesResponse {
                ok: false,
                usbmuxd_available: false,
                devices: Vec::new(),
                message: msg.clone(),
                error: Some(msg),
            };
        }
    };

    let raw_devices = match tokio::time::timeout(LOCKDOWN_TIMEOUT, conn.get_devices()).await {
        Ok(Ok(list)) => list,
        Ok(Err(e)) => {
            let msg = format!(
                "Не удалось получить список устройств от usbmuxd: {}",
                sanitize_message(&e.to_string())
            );
            return DevicesResponse {
                ok: false,
                usbmuxd_available: true,
                devices: Vec::new(),
                message: msg.clone(),
                error: Some(msg),
            };
        }
        Err(_) => {
            let msg = "Таймаут запроса списка устройств от usbmuxd.".to_string();
            return DevicesResponse {
                ok: false,
                usbmuxd_available: true,
                devices: Vec::new(),
                message: msg.clone(),
                error: Some(msg),
            };
        }
    };

    let deduped = dedupe_devices_prefer_usb(raw_devices);
    let mut entries = Vec::with_capacity(deduped.len());
    for dev in &deduped {
        entries.push(probe_device_info(&addr, dev).await);
    }

    let message = if entries.is_empty() {
        "Подключённые устройства не найдены.".to_string()
    } else {
        format!("Найдено устройств: {}", entries.len())
    };
    DevicesResponse {
        ok: true,
        usbmuxd_available: true,
        devices: entries,
        message,
        error: None,
    }
}

async fn select_device(udid_filter: Option<&str>) -> Result<(UsbmuxdAddr, UsbmuxdDevice), String> {
    let addr = usbmuxd_addr();
    let mut conn = tokio::time::timeout(LOCKDOWN_TIMEOUT, addr.connect(0))
        .await
        .map_err(|_| "таймаут подключения к usbmuxd".to_string())?
        .map_err(|_| {
            "служба usbmuxd не запущена или недоступна — подключите iPhone и запустите usbmuxd"
                .to_string()
        })?;
    let raw = tokio::time::timeout(LOCKDOWN_TIMEOUT, conn.get_devices())
        .await
        .map_err(|_| "таймаут чтения списка устройств".to_string())?
        .map_err(|e| format!("ошибка usbmuxd: {}", sanitize_message(&e.to_string())))?;
    let devices = dedupe_devices_prefer_usb(raw);

    if let Some(target) = udid_filter.map(str::trim).filter(|s| !s.is_empty()) {
        devices
            .into_iter()
            .find(|d| d.udid.eq_ignore_ascii_case(target))
            .map(|d| (addr, d))
            .ok_or_else(|| format!("устройство с UDID {target} не найдено по USB"))
    } else {
        let usb_only: Vec<UsbmuxdDevice> = devices
            .iter()
            .filter(|d| d.connection_type == Connection::Usb)
            .cloned()
            .collect();
        let pool = if !usb_only.is_empty() {
            usb_only
        } else {
            devices
        };
        match pool.len() {
            0 => Err("подключённый iPhone не найден — подключите устройство по USB и разблокируйте экран".into()),
            1 => Ok((addr, pool.into_iter().next().unwrap())),
            n => Err(format!(
                "подключено несколько устройств ({n}); выберите устройство (флаг --udid)"
            )),
        }
    }
}

struct SignedBundleCleanup {
    temp_dir_to_remove: Option<PathBuf>,
}

fn safe_extracted_temp_dir(input_is_file: bool, signed_app_dir: &Path) -> Option<PathBuf> {
    if !input_is_file {
        return None;
    }
    let tmp_root = fs::canonicalize(std::env::temp_dir()).ok()?;
    let canon_app = fs::canonicalize(signed_app_dir).ok()?;
    if !canon_app.starts_with(&tmp_root) || canon_app == tmp_root {
        return None;
    }
    if let Some(payload_dir) = canon_app.parent() {
        if payload_dir.file_name().and_then(|s| s.to_str()) == Some("Payload") {
            if let Some(extracted_dir) = payload_dir.parent() {
                if extracted_dir.parent() == Some(tmp_root.as_path())
                    && extracted_dir
                        .file_name()
                        .and_then(|s| s.to_str())
                        .is_some_and(|n| n.ends_with("_extracted"))
                {
                    return Some(extracted_dir.to_path_buf());
                }
            }
        }
    }
    Some(canon_app)
}

impl SignedBundleCleanup {
    fn new(input_is_file: bool, signed_app_dir: &Path) -> Self {
        Self {
            temp_dir_to_remove: safe_extracted_temp_dir(input_is_file, signed_app_dir),
        }
    }
}

impl Drop for SignedBundleCleanup {
    fn drop(&mut self) {
        if let Some(ref dir) = self.temp_dir_to_remove {
            let _ = fs::remove_dir_all(dir);
        }
    }
}

fn read_signed_bundle_metadata(app_dir: &Path) -> (Option<String>, Option<String>) {
    let info_path = app_dir.join("Info.plist");
    let Ok(dict) = plist::from_file::<_, plist::Dictionary>(&info_path) else {
        return (None, None);
    };
    let bundle_id = dict
        .get("CFBundleIdentifier")
        .and_then(plist::Value::as_string)
        .map(ToOwned::to_owned);
    let app_name = dict
        .get("CFBundleDisplayName")
        .or_else(|| dict.get("CFBundleName"))
        .and_then(plist::Value::as_string)
        .map(ToOwned::to_owned);
    (bundle_id, app_name)
}

pub fn inspect_ipa(ipa_path: &Path) -> InspectIpaResponse {
    let meta = match fs::metadata(ipa_path) {
        Ok(m) => m,
        Err(_) => {
            let msg = format!("Файл .ipa не найден: {}", ipa_path.display());
            return InspectIpaResponse {
                ok: false,
                bundle_id: None,
                app_name: None,
                version: None,
                min_os_version: None,
                extensions: Vec::new(),
                special_app: None,
                message: msg.clone(),
                error: Some(msg),
            };
        }
    };
    if !meta.is_file() && !meta.is_dir() {
        let msg = "Указанный путь не является .ipa файлом или .app каталогом.".to_string();
        return InspectIpaResponse {
            ok: false,
            bundle_id: None,
            app_name: None,
            version: None,
            min_os_version: None,
            extensions: Vec::new(),
            special_app: None,
            message: msg.clone(),
            error: Some(msg),
        };
    }

    let app = match Application::new(ipa_path.to_path_buf()) {
        Ok(a) => a,
        Err(e) => {
            let msg = format!(
                "isideload не смог разобрать IPA: {}",
                sanitize_message(&e.to_string())
            );
            return InspectIpaResponse {
                ok: false,
                bundle_id: None,
                app_name: None,
                version: None,
                min_os_version: None,
                extensions: Vec::new(),
                special_app: None,
                message: msg.clone(),
                error: Some(msg),
            };
        }
    };

    let _cleanup = SignedBundleCleanup::new(meta.is_file(), &app.bundle.bundle_dir);
    let bundle_id = app.main_bundle_id().ok();
    let app_name = app
        .bundle
        .app_info
        .get("CFBundleDisplayName")
        .or_else(|| app.bundle.app_info.get("CFBundleName"))
        .and_then(plist::Value::as_string)
        .map(ToOwned::to_owned)
        .or_else(|| app.main_app_name().ok());
    let version = app
        .bundle
        .app_info
        .get("CFBundleShortVersionString")
        .or_else(|| app.bundle.app_info.get("CFBundleVersion"))
        .and_then(plist::Value::as_string)
        .map(ToOwned::to_owned);
    let min_os_version = app
        .bundle
        .app_info
        .get("MinimumOSVersion")
        .and_then(plist::Value::as_string)
        .map(ToOwned::to_owned);
    let extensions: Vec<String> = app
        .bundle
        .app_extensions()
        .iter()
        .filter_map(|ext| ext.bundle_identifier().map(ToOwned::to_owned))
        .collect();
    let special_app = app.get_special_app().map(|s| s.to_string());

    let Some(ref bid) = bundle_id else {
        let msg = "В Info.plist отсутствует CFBundleIdentifier.".to_string();
        return InspectIpaResponse {
            ok: false,
            bundle_id: None,
            app_name,
            version,
            min_os_version,
            extensions,
            special_app,
            message: msg.clone(),
            error: Some(msg),
        };
    };

    let message = format!(
        "IPA проверен (isideload): {} ({bid}, v{})",
        app_name.as_deref().unwrap_or(bid),
        version.as_deref().unwrap_or("?")
    );
    InspectIpaResponse {
        ok: true,
        bundle_id: Some(bid.clone()),
        app_name,
        version,
        min_os_version,
        extensions,
        special_app,
        message,
        error: None,
    }
}

pub async fn install_ipa(
    ipa_path: &Path,
    session_dir: &Path,
    udid: Option<&str>,
    team_filter: Option<&str>,
    cert_policy: CertPolicy,
    ext_policy: ExtensionPolicy,
    emitter: &Emitter,
) -> Result<StreamEvent, (String, String)> {
    let ipa_meta = fs::metadata(ipa_path).map_err(|_| {
        (
            "connecting".to_string(),
            format!("файл .ipa не найден: {}", ipa_path.display()),
        )
    })?;
    if !ipa_meta.is_file() && !ipa_meta.is_dir() {
        return Err((
            "connecting".to_string(),
            "указанный путь .ipa не является файлом или .app каталогом".to_string(),
        ));
    }

    emitter.progress("connecting", 5, "Подключение к iPhone по USB...");
    let (addr, device) = select_device(udid)
        .await
        .map_err(|e| ("connecting".to_string(), e))?;
    let provider = device.to_provider(addr, "DopamineLoader");
    let device_info = tokio::time::timeout(SERVICE_TIMEOUT, IdeviceInfo::from_device(&provider))
        .await
        .map_err(|_| {
            (
                "connecting".to_string(),
                "таймаут подключения к lockdown iPhone; разблокируйте устройство и нажмите «Доверять»".to_string(),
            )
        })?
        .map_err(|e| {
            (
                "connecting".to_string(),
                format!(
                    "не удалось установить доверенную сессию с iPhone: {}",
                    sanitize_message(&e.to_string())
                ),
            )
        })?;

    let mut pairing_bytes = provider
        .get_pairing_file()
        .await
        .ok()
        .and_then(|mut p| {
            if p.udid.is_none() {
                p.udid = Some(device_info.udid.clone());
            }
            p.serialize().ok()
        });

    emitter.progress("authenticating", 14, "Загрузка сессии Apple ID...");
    let (mut account, email) = load_saved_account(session_dir)
        .await
        .map_err(|e| ("authenticating".to_string(), e))?;
    let dev_session = DeveloperSession::from_account(&mut account)
        .await
        .map_err(|_| {
            (
                "authenticating".to_string(),
                "сессия Apple ID истекла или недействительна — выполните вход заново".to_string(),
            )
        })?;

    let max_certs_behavior = match cert_policy {
        CertPolicy::Error => MaxCertsBehavior::Error,
        CertPolicy::Revoke => MaxCertsBehavior::Revoke,
    };
    let extensions_behavior = match ext_policy {
        ExtensionPolicy::RegisterAll => {
            ExtensionsBehaviorSelection::Fixed(ExtensionsBehavior::RegisterAll)
        }
        ExtensionPolicy::ReuseMain => {
            ExtensionsBehaviorSelection::Fixed(ExtensionsBehavior::ReuseMain)
        }
    };

    let mut sideloader =
        SideloaderBuilder::<MaxCertsCallbackBox, ExtensionsBehaviorCallbackBox>::new(
            dev_session,
            email,
        )
        .storage(Box::new(PrivateFsStorage::new(session_dir.to_path_buf())))
        .machine_name("DopamineLoader".to_string())
        .team_selection(TeamSelection::First)
        .max_certs_behavior(max_certs_behavior)
        .extensions_behavior(extensions_behavior)
        .delete_app_after_install(true)
        .build();

    emitter.progress("authenticating", 20, "Проверка команды Apple Developer и устройства...");
    let team = if let Some(tid) = team_filter.map(str::trim).filter(|s| !s.is_empty()) {
        sideloader
            .get_dev_session()
            .list_teams()
            .await
            .map_err(|e| {
                (
                    "authenticating".to_string(),
                    format!(
                        "не удалось получить список команд Apple Developer: {}",
                        sanitize_message(&e.to_string())
                    ),
                )
            })?
            .into_iter()
            .find(|t| t.team_id == tid)
            .ok_or_else(|| {
                (
                    "authenticating".to_string(),
                    format!("команда Apple Developer {tid} не найдена в аккаунте"),
                )
            })?
    } else {
        sideloader.get_team().await.map_err(|e| {
            (
                "authenticating".to_string(),
                format!(
                    "не удалось выбрать команду Apple Developer: {}",
                    sanitize_message(&e.to_string())
                ),
            )
        })?
    };

    sideloader
        .get_dev_session()
        .ensure_device_registered(&team, &device_info.name, &device_info.udid, None)
        .await
        .map_err(|e| {
            (
                "authenticating".to_string(),
                format!(
                    "не удалось зарегистрировать iPhone в Apple Developer: {}",
                    sanitize_message(&e.to_string())
                ),
            )
        })?;

    emitter.progress("signing", 25, "Подготовка и подпись .ipa...");
    let sign_res = sideloader
        .sign_app(
            ipa_path.to_path_buf(),
            Some(team),
            false,
            Some(|frac: f32| async move {
                let clamped = frac.clamp(0.0, 1.0);
                let pct = 25u8.saturating_add((clamped * 35.0).round() as u8).min(60);
                emitter.progress("signing", pct, "Подпись приложения и расширений...");
            }),
            pairing_bytes.as_deref(),
            Some(&device_info.udid),
        )
        .await;

    if let Some(ref mut bytes) = pairing_bytes {
        bytes.zeroize();
    }

    let (signed_app_path, special_app) = sign_res.map_err(|e| {
        let raw = e.to_string();
        let hint = if raw.contains("7460") || raw.contains("Maximum number of certificates") {
            "достигнут лимит сертификатов Apple Developer (передайте --max-certs revoke для перевыпуска)".to_string()
        } else {
            format!("ошибка подписи приложения: {}", sanitize_message(&raw))
        };
        ("signing".to_string(), hint)
    })?;

    let _cleanup = SignedBundleCleanup::new(ipa_meta.is_file(), &signed_app_path);
    let (bundle_id, app_name) = read_signed_bundle_metadata(&signed_app_path);

    emitter.progress("uploading", 60, "Загрузка подписанного приложения на iPhone...");
    isideload::sideload::install::install_app(&provider, &signed_app_path, |pct| {
        if pct <= 70 {
            let mapped = 60u8.saturating_add(((pct * 28) / 70) as u8).min(88);
            emitter.progress("uploading", mapped, "Загрузка файлов приложения на iPhone...");
        } else {
            let install_part = pct.saturating_sub(70).min(30);
            let mapped = 88u8.saturating_add(((install_part * 11) / 30) as u8).min(99);
            emitter.progress("installing", mapped, "Установка приложения на iPhone...");
        }
    })
    .await
    .map_err(|e| {
        (
            "installing".to_string(),
            format!(
                "ошибка установки на iPhone: {}",
                sanitize_message(&e.to_string())
            ),
        )
    })?;

    let display = app_name
        .clone()
        .or_else(|| bundle_id.clone())
        .unwrap_or_else(|| "приложение".to_string());
    Ok(StreamEvent::Done {
        ok: true,
        email_masked: None,
        bundle_id,
        app_name,
        udid: Some(device_info.udid),
        special_app: special_app.map(|s| s.to_string()),
        team_id: None,
        expires_at_unix: None,
        hours_remaining: None,
        saved_path: None,
        remote_path: None,
        synced_session: None,
        message: format!("Приложение {display} успешно установлено на iPhone."),
    })
}

fn is_sidebox_bundle_id(bundle: &str) -> bool {
    bundle == "com.bromhobit.Sidebox"
        || bundle.starts_with("com.bromhobit.Sidebox.")
        || bundle == "org.codex.SideBox"
        || bundle.starts_with("org.codex.SideBox.")
}

fn detect_sidebox_bundle_from_apps(apps: &HashMap<String, plist::Value>) -> Option<String> {
    let mut new_suffixed: Vec<&String> = apps
        .keys()
        .filter(|id| {
            id.strip_prefix("com.bromhobit.Sidebox.")
                .is_some_and(|team| !team.is_empty() && !team.contains('.'))
        })
        .collect();
    new_suffixed.sort();
    if let Some(id) = new_suffixed.first() {
        return Some((*id).clone());
    }
    if apps.contains_key("com.bromhobit.Sidebox") {
        return Some("com.bromhobit.Sidebox".to_string());
    }

    let mut legacy_suffixed: Vec<&String> = apps
        .keys()
        .filter(|id| {
            id.strip_prefix("org.codex.SideBox.")
                .is_some_and(|team| !team.is_empty() && !team.contains('.'))
        })
        .collect();
    legacy_suffixed.sort();
    if let Some(id) = legacy_suffixed.first() {
        return Some((*id).clone());
    }
    if apps.contains_key("org.codex.SideBox") {
        return Some("org.codex.SideBox".to_string());
    }

    for (id, info) in apps {
        let is_sidebox = info
            .as_dictionary()
            .and_then(|d| {
                d.get("CFBundleDisplayName")
                    .or_else(|| d.get("CFBundleName"))
            })
            .and_then(plist::Value::as_string)
            .is_some_and(|n| n.eq_ignore_ascii_case("Sidebox"));
        if is_sidebox {
            return Some(id.clone());
        }
    }
    None
}

fn detect_sidestore_bundle_from_apps(apps: &HashMap<String, plist::Value>) -> Option<String> {
    // Prefer team-suffixed SideStore bundle (com.SideStore.SideStore.<TEAM>)
    let mut suffixed: Vec<&String> = apps
        .keys()
        .filter(|id| id.starts_with("com.SideStore.SideStore."))
        .collect();
    suffixed.sort();
    if let Some(id) = suffixed.first() {
        return Some((*id).clone());
    }
    if apps.contains_key("com.SideStore.SideStore") {
        return Some("com.SideStore.SideStore".to_string());
    }
    for (id, info) in apps {
        let is_sidestore = info
            .as_dictionary()
            .and_then(|d| {
                d.get("CFBundleName")
                    .or_else(|| d.get("CFBundleDisplayName"))
            })
            .and_then(plist::Value::as_string)
            .is_some_and(|n| n.eq_ignore_ascii_case("SideStore"));
        if is_sidestore {
            return Some(id.clone());
        }
    }
    None
}

fn resolve_target_app_from_list(
    apps: &HashMap<String, plist::Value>,
    target: PairingTarget,
    has_sync_session: bool,
) -> Option<(String, &'static str)> {
    match target {
        PairingTarget::SideBox => detect_sidebox_bundle_from_apps(apps)
            .map(|b| (b, PAIRING_REMOTE_PATH_SIDEBOX)),
        PairingTarget::SideStore => detect_sidestore_bundle_from_apps(apps)
            .map(|b| (b, PAIRING_REMOTE_PATH_SIDESTORE)),
        PairingTarget::Auto => {
            if has_sync_session {
                detect_sidebox_bundle_from_apps(apps)
                    .map(|b| (b, PAIRING_REMOTE_PATH_SIDEBOX))
                    .or_else(|| {
                        detect_sidestore_bundle_from_apps(apps)
                            .map(|b| (b, PAIRING_REMOTE_PATH_SIDESTORE))
                    })
            } else {
                detect_sidestore_bundle_from_apps(apps)
                    .map(|b| (b, PAIRING_REMOTE_PATH_SIDESTORE))
                    .or_else(|| {
                        detect_sidebox_bundle_from_apps(apps)
                            .map(|b| (b, PAIRING_REMOTE_PATH_SIDEBOX))
                    })
            }
        }
    }
}

fn remote_pairing_path_for_bundle(bundle: &str, target: PairingTarget) -> &'static str {
    if target == PairingTarget::SideBox || is_sidebox_bundle_id(bundle) {
        PAIRING_REMOTE_PATH_SIDEBOX
    } else {
        PAIRING_REMOTE_PATH_SIDESTORE
    }
}

struct SessionSyncPayload {
    session_bytes: Vec<u8>,
    anisette_bytes: Option<Vec<u8>>,
}

impl Drop for SessionSyncPayload {
    fn drop(&mut self) {
        self.session_bytes.zeroize();
        if let Some(ref mut a) = self.anisette_bytes {
            a.zeroize();
        }
    }
}

fn read_private_session_bundle_for_sync(dir: &Path) -> Result<SessionSyncPayload, String> {
    use std::os::unix::fs::PermissionsExt;
    ensure_private_dir(dir, false)?;
    let session_path = dir.join("apple-session.plist");
    let meta = fs::symlink_metadata(&session_path)
        .map_err(|_| "файл apple-session.plist не найден в каталоге сессии".to_string())?;
    if meta.file_type().is_symlink() || !meta.is_file() || meta.permissions().mode() & 0o077 != 0 {
        return Err("apple-session.plist должен быть обычным файлом с правами 0600".into());
    }
    if meta.len() == 0 || meta.len() > MAX_SYNC_FILE_BYTES {
        return Err("недопустимый размер apple-session.plist".into());
    }
    let mut session_bytes =
        fs::read(&session_path).map_err(|_| "не удалось прочитать apple-session.plist".to_string())?;
    let parsed: Result<(String, plist::Dictionary), _> = plist::from_bytes(&session_bytes);
    match parsed {
        Ok((email, spd)) if !email.trim().is_empty() && !spd.is_empty() => {
            drop(email);
            drop(spd);
        }
        _ => {
            session_bytes.zeroize();
            return Err("некорректный или неполный файл apple-session.plist".into());
        }
    }

    let anisette_path = dir.join("anisette_state");
    let anisette_bytes = match fs::symlink_metadata(&anisette_path) {
        Ok(m) if !m.file_type().is_symlink() && m.is_file() && m.len() > 0 && m.len() <= MAX_SYNC_FILE_BYTES => {
            fs::read(&anisette_path).ok()
        }
        _ => None,
    };

    Ok(SessionSyncPayload {
        session_bytes,
        anisette_bytes,
    })
}

async fn afc_write_file(
    afc: &mut idevice::afc::AfcClient,
    remote_path: &str,
    data: &[u8],
) -> Result<(), String> {
    let mut fd = afc
        .open(remote_path, AfcFopenMode::WrOnly)
        .await
        .map_err(|e| {
            format!(
                "не удалось открыть {remote_path} через AFC: {}",
                sanitize_message(&e.to_string())
            )
        })?;
    let write_res = fd.write_entire(data).await;
    let close_res = fd.close().await;
    write_res.and(close_res).map_err(|e| {
        format!(
            "ошибка записи {remote_path} на iPhone: {}",
            sanitize_message(&e.to_string())
        )
    })
}

pub async fn install_pairing(
    udid: Option<&str>,
    bundle_opt: Option<&str>,
    target: PairingTarget,
    sync_session_dir: Option<&Path>,
    save_path: Option<&Path>,
    emitter: &Emitter,
) -> Result<StreamEvent, (String, String)> {
    if let Some(bundle) = bundle_opt {
        validate_bundle_id(bundle).map_err(|e| ("connecting".to_string(), e))?;
    }

    let mut session_sync_payload: Option<SessionSyncPayload> =
        if let Some(dir) = sync_session_dir {
            Some(
                read_private_session_bundle_for_sync(dir)
                    .map_err(|e| ("connecting".to_string(), e))?,
            )
        } else {
            None
        };

    emitter.progress("connecting", 15, "Получение pairing-записи устройства из usbmuxd...");
    let (addr, device) = select_device(udid)
        .await
        .map_err(|e| ("connecting".to_string(), e))?;
    let provider = device.to_provider(addr, "DopamineLoader");

    let mut pairing = tokio::time::timeout(LOCKDOWN_TIMEOUT, provider.get_pairing_file())
        .await
        .map_err(|_| {
            (
                "connecting".to_string(),
                "таймаут чтения pairing-записи из usbmuxd".to_string(),
            )
        })?
        .map_err(|_| {
            (
                "connecting".to_string(),
                "pairing-запись не найдена — разблокируйте iPhone и нажмите «Доверять этому компьютеру»".to_string(),
            )
        })?;
    if pairing.udid.is_none() {
        pairing.udid = Some(device.udid.clone());
    }
    let mut pairing_xml = pairing.serialize().map_err(|e| {
        (
            "connecting".to_string(),
            format!(
                "не удалось сериализовать pairing-файл: {}",
                sanitize_message(&e.to_string())
            ),
        )
    })?;

    let mut saved_path_str = None;
    if let Some(path) = save_path {
        emitter.progress("uploading", 35, "Сохранение локального pairing-файла (0600)...");
        if let Err(err) = write_private_atomic(path, &pairing_xml, true) {
            pairing_xml.zeroize();
            return Err(("uploading".to_string(), err));
        }
        saved_path_str = Some(path.display().to_string());
    }

    let should_push =
        bundle_opt.is_some() || save_path.is_none() || sync_session_dir.is_some() || target != PairingTarget::Auto;
    let mut pushed_bundle = None;
    let mut remote_path = None;
    let mut synced_session = None;

    if should_push {
        let (target_bundle, pairing_dest) = if let Some(b) = bundle_opt {
            (b.to_string(), remote_pairing_path_for_bundle(b, target))
        } else {
            emitter.progress("connecting", 40, "Поиск целевого приложения на iPhone...");
            let mut instproxy = tokio::time::timeout(
                SERVICE_TIMEOUT,
                InstallationProxyClient::connect(&provider),
            )
            .await
            .map_err(|_| {
                pairing_xml.zeroize();
                (
                    "connecting".to_string(),
                    "таймаут подключения к installation_proxy".to_string(),
                )
            })?
            .map_err(|e| {
                pairing_xml.zeroize();
                (
                    "connecting".to_string(),
                    format!(
                        "не удалось подключиться к installation_proxy: {}",
                        sanitize_message(&e.to_string())
                    ),
                )
            })?;
            let apps = tokio::time::timeout(
                SERVICE_TIMEOUT,
                instproxy.get_apps(Some("User"), None),
            )
            .await
            .map_err(|_| {
                pairing_xml.zeroize();
                (
                    "connecting".to_string(),
                    "таймаут получения списка приложений iPhone".to_string(),
                )
            })?
            .map_err(|e| {
                pairing_xml.zeroize();
                (
                    "connecting".to_string(),
                    format!(
                        "не удалось получить список приложений iPhone: {}",
                        sanitize_message(&e.to_string())
                    ),
                )
            })?;
            match resolve_target_app_from_list(&apps, target, sync_session_dir.is_some()) {
                Some(pair) => pair,
                None => {
                    pairing_xml.zeroize();
                    let expected = match target {
                        PairingTarget::SideBox => "Sidebox",
                        PairingTarget::SideStore => "SideStore",
                        PairingTarget::Auto if sync_session_dir.is_some() => "Sidebox",
                        PairingTarget::Auto => "SideStore или Sidebox",
                    };
                    return Err((
                        "connecting".to_string(),
                        format!(
                            "{expected} не найден среди установленных приложений; сначала установите .ipa или укажите --bundle вручную"
                        ),
                    ));
                }
            }
        };

        emitter.progress(
            "uploading",
            60,
            format!("Передача pairing-файла в контейнер {target_bundle}..."),
        );

        let ha_client = tokio::time::timeout(SERVICE_TIMEOUT, HouseArrestClient::connect(&provider))
            .await
            .map_err(|_| {
                pairing_xml.zeroize();
                (
                    "uploading".to_string(),
                    "таймаут подключения к house_arrest".to_string(),
                )
            })?
            .map_err(|e| {
                pairing_xml.zeroize();
                (
                    "uploading".to_string(),
                    format!(
                        "не удалось подключиться к house_arrest: {}",
                        sanitize_message(&e.to_string())
                    ),
                )
            })?;

        let mut afc = match ha_client.vend_documents(&target_bundle).await {
            Ok(client) => client,
            Err(_) => {
                let ha_retry =
                    tokio::time::timeout(SERVICE_TIMEOUT, HouseArrestClient::connect(&provider))
                        .await
                        .map_err(|_| {
                            pairing_xml.zeroize();
                            (
                                "uploading".to_string(),
                                "таймаут повторного подключения к house_arrest".to_string(),
                            )
                        })?
                        .map_err(|e| {
                            pairing_xml.zeroize();
                            (
                                "uploading".to_string(),
                                format!(
                                    "house_arrest недоступен: {}",
                                    sanitize_message(&e.to_string())
                                ),
                            )
                        })?;
                ha_retry.vend_container(&target_bundle).await.map_err(|e| {
                    pairing_xml.zeroize();
                    (
                        "uploading".to_string(),
                        format!(
                            "iOS отклонила доступ к контейнеру {target_bundle} (разблокируйте iPhone и проверьте bundle ID): {}",
                            sanitize_message(&e.to_string())
                        ),
                    )
                })?
            }
        };

        if let Err(err) = afc_write_file(&mut afc, pairing_dest, &pairing_xml).await {
            pairing_xml.zeroize();
            return Err(("uploading".to_string(), err));
        }

        if let Some(payload) = session_sync_payload.take() {
            emitter.progress(
                "uploading",
                82,
                format!("Синхронизация сессии Apple ID в {target_bundle}..."),
            );
            if let Err(err) = afc_write_file(&mut afc, SESSION_REMOTE_PATH, &payload.session_bytes).await {
                pairing_xml.zeroize();
                return Err(("uploading".to_string(), err));
            }
            if let Some(ref anisette_bytes) = payload.anisette_bytes {
                if let Err(err) = afc_write_file(&mut afc, ANISETTE_REMOTE_PATH, anisette_bytes).await {
                    pairing_xml.zeroize();
                    return Err(("uploading".to_string(), err));
                }
            }
            synced_session = Some(true);
        }

        pushed_bundle = Some(target_bundle);
        remote_path = Some(pairing_dest.to_string());
    }

    pairing_xml.zeroize();
    drop(session_sync_payload);

    let message = match (&pushed_bundle, &saved_path_str, synced_session) {
        (Some(b), _, Some(true)) => {
            format!("Pairing-файл и сессия Apple ID успешно переданы в {b}.")
        }
        (Some(b), Some(p), _) => {
            format!("Pairing-файл загружен в {b} и сохранён в {p}.")
        }
        (Some(b), None, _) => format!("Pairing-файл успешно загружен в {b}."),
        (None, Some(p), _) => format!("Pairing-файл сохранён в {p} (права 0600)."),
        (None, None, _) => "Готово.".to_string(),
    };
    Ok(StreamEvent::Done {
        ok: true,
        email_masked: None,
        bundle_id: pushed_bundle,
        app_name: None,
        udid: Some(device.udid),
        special_app: None,
        team_id: None,
        expires_at_unix: None,
        hours_remaining: None,
        saved_path: saved_path_str,
        remote_path,
        synced_session,
        message,
    })
}

async fn read_device_profiles(
    provider: &impl IdeviceProvider,
) -> Result<Vec<plist::Dictionary>, String> {
    let mut misagent = tokio::time::timeout(SERVICE_TIMEOUT, MisagentClient::connect(provider))
        .await
        .map_err(|_| "таймаут подключения к службе misagent на iPhone".to_string())?
        .map_err(|e| {
            format!(
                "не удалось подключиться к misagent на iPhone: {}",
                sanitize_message(&e.to_string())
            )
        })?;
    let raw_profiles = tokio::time::timeout(SERVICE_TIMEOUT, misagent.copy_all())
        .await
        .map_err(|_| "таймаут чтения профилей подписи с iPhone".to_string())?
        .map_err(|e| {
            format!(
                "не удалось прочитать профили подписи с iPhone: {}",
                sanitize_message(&e.to_string())
            )
        })?;
    let mut parsed = Vec::new();
    for blob in &raw_profiles {
        if let Ok(dict) = parse_cms_profile_dict(blob) {
            parsed.push(dict);
        }
    }
    Ok(parsed)
}

pub async fn renew_usb(
    session_dir: &Path,
    udid: Option<&str>,
    bundle_id: &str,
    team_opt: Option<&str>,
    emitter: &Emitter,
) -> Result<StreamEvent, (String, String)> {
    validate_bundle_id(bundle_id).map_err(|e| ("connecting".to_string(), e))?;

    emitter.progress("connecting", 10, "Подключение к iPhone и чтение текущего профиля...");
    let (addr, device) = select_device(udid)
        .await
        .map_err(|e| ("connecting".to_string(), e))?;
    let provider = device.to_provider(addr, "DopamineLoader");

    let current_profiles = read_device_profiles(&provider)
        .await
        .map_err(|e| ("connecting".to_string(), e))?;
    let old_profile = find_newest_matching_profile(&current_profiles, bundle_id, team_opt)
        .ok_or_else(|| {
            (
                "connecting".to_string(),
                format!("установленный профиль для {bundle_id} не найден на iPhone"),
            )
        })?
        .clone();

    let team_id = team_opt
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(ToOwned::to_owned)
        .or_else(|| profile_team_id(&old_profile))
        .ok_or_else(|| {
            (
                "connecting".to_string(),
                "не удалось определить Team ID из установленного профиля".to_string(),
            )
        })?;

    emitter.progress("authenticating", 30, "Запрос нового профиля подписи у Apple...");
    let (mut account, _email) = load_saved_account(session_dir)
        .await
        .map_err(|e| ("authenticating".to_string(), e))?;
    let mut session = DeveloperSession::from_account(&mut account)
        .await
        .map_err(|_| {
            (
                "authenticating".to_string(),
                "сессия Apple ID истекла или недействительна — выполните повторный вход"
                    .to_string(),
            )
        })?;

    let team = session
        .list_teams()
        .await
        .map_err(|e| {
            (
                "authenticating".to_string(),
                format!(
                    "не удалось получить список команд Apple Developer: {}",
                    sanitize_message(&e.to_string())
                ),
            )
        })?
        .into_iter()
        .find(|t| t.team_id == team_id)
        .ok_or_else(|| {
            (
                "authenticating".to_string(),
                format!("команда {team_id} не найдена в аккаунте Apple ID"),
            )
        })?;

    let app = session
        .list_app_ids(&team, DeveloperDeviceType::Ios)
        .await
        .map_err(|e| {
            (
                "authenticating".to_string(),
                format!(
                    "не удалось получить список App ID: {}",
                    sanitize_message(&e.to_string())
                ),
            )
        })?
        .app_ids
        .into_iter()
        .find(|a| a.identifier == bundle_id)
        .ok_or_else(|| {
            (
                "authenticating".to_string(),
                format!("App ID {bundle_id} не зарегистрирован в команде {team_id}"),
            )
        })?;

    emitter.progress("signing", 55, "Скачивание и проверка совместимости нового профиля...");
    let fresh = session
        .download_team_provisioning_profile(&team, &app, DeveloperDeviceType::Ios)
        .await
        .map_err(|e| {
            (
                "signing".to_string(),
                format!(
                    "Apple не выдала новый профиль: {}",
                    sanitize_message(&e.to_string())
                ),
            )
        })?;

    let new_bytes: Vec<u8> = fresh.encoded_profile.into();
    let new_dict =
        parse_cms_profile_dict(&new_bytes).map_err(|e| ("signing".to_string(), e))?;
    let now = now_unix();
    let expected_expiry = check_replacement_compatible(
        &old_profile,
        &new_dict,
        &team_id,
        bundle_id,
        &device.udid,
        now,
    )
    .map_err(|e| ("signing".to_string(), e))?;

    emitter.progress("installing", 80, "Установка нового профиля на iPhone через misagent...");
    let mut misagent = tokio::time::timeout(SERVICE_TIMEOUT, MisagentClient::connect(&provider))
        .await
        .map_err(|_| {
            (
                "installing".to_string(),
                "таймаут подключения к misagent для установки профиля".to_string(),
            )
        })?
        .map_err(|e| {
            (
                "installing".to_string(),
                format!(
                    "не удалось подключиться к misagent: {}",
                    sanitize_message(&e.to_string())
                ),
            )
        })?;
    tokio::time::timeout(SERVICE_TIMEOUT, misagent.install(new_bytes))
        .await
        .map_err(|_| {
            (
                "installing".to_string(),
                "таймаут установки профиля на iPhone".to_string(),
            )
        })?
        .map_err(|e| {
            (
                "installing".to_string(),
                format!(
                    "iOS отклонила установку профиля: {}",
                    sanitize_message(&e.to_string())
                ),
            )
        })?;

    emitter.progress("installing", 92, "Проверка применённого профиля на iPhone...");
    let after_profiles = read_device_profiles(&provider)
        .await
        .map_err(|e| ("installing".to_string(), e))?;
    let installed = find_newest_matching_profile(&after_profiles, bundle_id, Some(&team_id))
        .ok_or_else(|| {
            (
                "installing".to_string(),
                "не удалось найти установленный профиль после замены".to_string(),
            )
        })?;
    let installed_expiry = check_replacement_compatible(
        &old_profile,
        installed,
        &team_id,
        bundle_id,
        &device.udid,
        now,
    )
    .map_err(|e| ("installing".to_string(), e))?;
    if installed_expiry < expected_expiry || installed != &new_dict {
        return Err((
            "installing".to_string(),
            "iOS не подтвердила замену профиля на новый".to_string(),
        ));
    }

    let hours_remaining = installed_expiry.saturating_sub(now_unix()) / 3600;
    Ok(StreamEvent::Done {
        ok: true,
        email_masked: None,
        bundle_id: Some(bundle_id.to_string()),
        app_name: None,
        udid: Some(device.udid),
        special_app: None,
        team_id: Some(team_id),
        expires_at_unix: Some(installed_expiry),
        hours_remaining: Some(hours_remaining),
        saved_path: None,
        remote_path: None,
        synced_session: None,
        message: format!("Профиль {bundle_id} продлён по USB (действует ещё ~{hours_remaining} ч)."),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::{IpAddr, Ipv4Addr};

    #[test]
    fn dedupe_prefers_usb_over_network() {
        let net = UsbmuxdDevice {
            connection_type: Connection::Network(IpAddr::V4(Ipv4Addr::LOCALHOST)),
            udid: "00000000-AAA".into(),
            device_id: 1,
        };
        let usb = UsbmuxdDevice {
            connection_type: Connection::Usb,
            udid: "00000000-aaa".into(),
            device_id: 2,
        };
        let out = dedupe_devices_prefer_usb(vec![net, usb]);
        assert_eq!(out.len(), 1);
        assert_eq!(out[0].connection_type, Connection::Usb);
        assert_eq!(out[0].device_id, 2);
    }

    #[test]
    fn sidestore_auto_detection_prefers_team_suffixed_bundle() {
        let mut apps = HashMap::new();
        apps.insert(
            "com.example.Other".into(),
            plist::Value::Dictionary(plist::Dictionary::new()),
        );
        apps.insert(
            "com.SideStore.SideStore".into(),
            plist::Value::Dictionary(plist::Dictionary::new()),
        );
        apps.insert(
            "com.SideStore.SideStore.TEAM123456".into(),
            plist::Value::Dictionary(plist::Dictionary::new()),
        );
        assert_eq!(
            detect_sidestore_bundle_from_apps(&apps).as_deref(),
            Some("com.SideStore.SideStore.TEAM123456")
        );
    }

    #[test]
    fn sidebox_detection_and_pairing_target_routing() {
        let mut apps = HashMap::new();
        apps.insert(
            "com.SideStore.SideStore.TEAM123456".into(),
            plist::Value::Dictionary(plist::Dictionary::new()),
        );
        apps.insert(
            "org.codex.SideBox.TEAM123456".into(),
            plist::Value::Dictionary(plist::Dictionary::new()),
        );

        // Legacy SideBox detection & pairing path
        assert_eq!(
            resolve_target_app_from_list(&apps, PairingTarget::SideBox, false),
            Some((
                "org.codex.SideBox.TEAM123456".into(),
                PAIRING_REMOTE_PATH_SIDEBOX
            ))
        );
        // Auto with --sync-session-dir prefers Sidebox
        assert_eq!(
            resolve_target_app_from_list(&apps, PairingTarget::Auto, true),
            Some((
                "org.codex.SideBox.TEAM123456".into(),
                PAIRING_REMOTE_PATH_SIDEBOX
            ))
        );
        // Auto without --sync-session-dir preserves SideStore default
        assert_eq!(
            resolve_target_app_from_list(&apps, PairingTarget::Auto, false),
            Some((
                "com.SideStore.SideStore.TEAM123456".into(),
                PAIRING_REMOTE_PATH_SIDESTORE
            ))
        );

        // Priority of com.bromhobit.Sidebox.<TEAM> when both new and legacy are installed
        apps.insert(
            "com.bromhobit.Sidebox.TEAM123456.Widget".into(),
            plist::Value::Dictionary(plist::Dictionary::new()),
        );
        apps.insert(
            "com.bromhobit.Sidebox.TEAM123456".into(),
            plist::Value::Dictionary(plist::Dictionary::new()),
        );
        assert_eq!(
            resolve_target_app_from_list(&apps, PairingTarget::SideBox, false),
            Some((
                "com.bromhobit.Sidebox.TEAM123456".into(),
                PAIRING_REMOTE_PATH_SIDEBOX
            ))
        );
        assert_eq!(
            resolve_target_app_from_list(&apps, PairingTarget::Auto, true),
            Some((
                "com.bromhobit.Sidebox.TEAM123456".into(),
                PAIRING_REMOTE_PATH_SIDEBOX
            ))
        );

        // New-only exact or suffixed bundle detection & pairing path
        let mut new_only = HashMap::new();
        new_only.insert(
            "com.bromhobit.Sidebox".into(),
            plist::Value::Dictionary(plist::Dictionary::new()),
        );
        assert_eq!(
            resolve_target_app_from_list(&new_only, PairingTarget::SideBox, false),
            Some(("com.bromhobit.Sidebox".into(), PAIRING_REMOTE_PATH_SIDEBOX))
        );

        assert_eq!(
            remote_pairing_path_for_bundle("com.bromhobit.Sidebox.TEAM123456", PairingTarget::Auto),
            PAIRING_REMOTE_PATH_SIDEBOX
        );
        assert_eq!(
            remote_pairing_path_for_bundle("com.bromhobit.Sidebox", PairingTarget::Auto),
            PAIRING_REMOTE_PATH_SIDEBOX
        );
        assert_eq!(
            remote_pairing_path_for_bundle("org.codex.SideBox.TEAM123456", PairingTarget::Auto),
            PAIRING_REMOTE_PATH_SIDEBOX
        );
    }

    #[test]
    fn signed_bundle_cleanup_never_deletes_user_app_directory() {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let extracted = std::env::temp_dir().join(format!(
            "dopamine-cleanup-{}-{nanos}.ipa_extracted",
            std::process::id()
        ));
        let app_dir = extracted.join("Payload").join("Test.app");
        fs::create_dir_all(&app_dir).unwrap();

        // When input is a directory (.app), cleanup MUST NOT remove it
        {
            let _guard = SignedBundleCleanup::new(false, &app_dir);
        }
        assert!(app_dir.exists(), "user .app dir must be preserved");

        // When input is an .ipa file extracted under temp_dir, cleanup removes the extracted dir
        {
            let _guard = SignedBundleCleanup::new(true, &app_dir);
        }
        assert!(
            !extracted.exists(),
            "extracted temp dir should be cleaned up"
        );
    }
}
