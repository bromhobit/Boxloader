use serde::{Deserialize, Serialize};
use std::{
    io::{self, Write},
    sync::Mutex,
};

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct DeviceEntry {
    pub udid: String,
    pub device_id: u32,
    pub connection_type: String,
    pub name: String,
    pub product_type: String,
    pub product_version: String,
    pub paired: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct DevicesResponse {
    pub ok: bool,
    pub usbmuxd_available: bool,
    pub devices: Vec<DeviceEntry>,
    pub message: String,
    pub error: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SessionStatusResponse {
    pub ok: bool,
    pub exists: bool,
    pub valid: bool,
    pub dir_exists: bool,
    pub dir_private: bool,
    pub session_private: bool,
    pub anisette_state_exists: bool,
    pub email_masked: Option<String>,
    pub message: String,
    pub error: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct InspectIpaResponse {
    pub ok: bool,
    pub bundle_id: Option<String>,
    pub app_name: Option<String>,
    pub version: Option<String>,
    pub min_os_version: Option<String>,
    pub extensions: Vec<String>,
    pub special_app: Option<String>,
    pub message: String,
    pub error: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "event", rename_all = "snake_case")]
pub enum StreamEvent {
    Progress {
        stage: String,
        percent: u8,
        message: String,
    },
    #[serde(rename = "need_2fa")]
    Need2fa {
        sms: bool,
        retry: bool,
        last_error: Option<String>,
        message: String,
    },
    Done {
        ok: bool,
        #[serde(skip_serializing_if = "Option::is_none")]
        email_masked: Option<String>,
        #[serde(skip_serializing_if = "Option::is_none")]
        bundle_id: Option<String>,
        #[serde(skip_serializing_if = "Option::is_none")]
        app_name: Option<String>,
        #[serde(skip_serializing_if = "Option::is_none")]
        udid: Option<String>,
        #[serde(skip_serializing_if = "Option::is_none")]
        special_app: Option<String>,
        #[serde(skip_serializing_if = "Option::is_none")]
        team_id: Option<String>,
        #[serde(skip_serializing_if = "Option::is_none")]
        expires_at_unix: Option<u64>,
        #[serde(skip_serializing_if = "Option::is_none")]
        hours_remaining: Option<u64>,
        #[serde(skip_serializing_if = "Option::is_none")]
        saved_path: Option<String>,
        #[serde(skip_serializing_if = "Option::is_none")]
        remote_path: Option<String>,
        #[serde(skip_serializing_if = "Option::is_none")]
        synced_session: Option<bool>,
        message: String,
    },
    Error {
        stage: String,
        message: String,
    },
}

pub struct Emitter {
    json: bool,
    lock: Mutex<Option<(String, u8)>>,
}

impl Emitter {
    pub fn new(json: bool) -> Self {
        Self {
            json,
            lock: Mutex::new(None),
        }
    }

    pub fn is_json(&self) -> bool {
        self.json
    }

    pub fn progress(&self, stage: &str, percent: u8, message: impl Into<String>) {
        let percent = percent.min(100);
        let message = sanitize_message(&message.into());
        let mut last = self.lock.lock().unwrap_or_else(|e| e.into_inner());
        if let Some((prev_stage, prev_pct)) = last.as_ref() {
            if prev_stage == stage && *prev_pct == percent {
                return;
            }
        }
        *last = Some((stage.to_string(), percent));
        drop(last);

        let ev = StreamEvent::Progress {
            stage: stage.to_string(),
            percent,
            message,
        };
        self.emit_event(&ev);
    }

    pub fn need_2fa(&self, sms: bool, retry: bool, last_error: Option<String>, message: &str) {
        let ev = StreamEvent::Need2fa {
            sms,
            retry,
            last_error: last_error.map(|s| sanitize_message(&s)),
            message: sanitize_message(message),
        };
        self.emit_event(&ev);
    }

    pub fn emit_event(&self, event: &StreamEvent) {
        let _guard = self.lock.lock().unwrap_or_else(|e| e.into_inner());
        if self.json {
            if let Ok(line) = serde_json::to_string(event) {
                let mut stdout = io::stdout().lock();
                let _ = writeln!(stdout, "{line}");
                let _ = stdout.flush();
            }
        } else {
            match event {
                StreamEvent::Progress {
                    stage,
                    percent,
                    message,
                } => {
                    let mut stdout = io::stdout().lock();
                    let _ = writeln!(stdout, "[{stage} {percent}%] {message}");
                    let _ = stdout.flush();
                }
                StreamEvent::Need2fa { message, .. } => {
                    let mut stdout = io::stdout().lock();
                    let _ = writeln!(stdout, "{message}");
                    let _ = stdout.flush();
                }
                StreamEvent::Done { message, .. } => {
                    let mut stdout = io::stdout().lock();
                    let _ = writeln!(stdout, "{message}");
                    let _ = stdout.flush();
                }
                StreamEvent::Error { stage, message } => {
                    let mut stderr = io::stderr().lock();
                    let _ = writeln!(stderr, "Ошибка ({stage}): {message}");
                    let _ = stderr.flush();
                }
            }
        }
    }

    pub fn error(&self, stage: &str, message: impl Into<String>) {
        let ev = StreamEvent::Error {
            stage: stage.to_string(),
            message: sanitize_message(&message.into()),
        };
        self.emit_event(&ev);
    }
}

pub fn mask_email(email: &str) -> String {
    let trimmed = email.trim();
    let Some((local, domain)) = trimmed.split_once('@') else {
        return "***".to_string();
    };
    let mut chars = local.chars();
    let first = chars.next();
    match (first, chars.next()) {
        (Some(ch), Some(_)) => format!("{ch}***@{domain}"),
        (Some(_), None) => format!("*@{domain}"),
        _ => format!("***@{domain}"),
    }
}

/// Remove control chars, truncate excessively long errors, and redact any obvious token fields.
pub fn sanitize_message(input: &str) -> String {
    let mut cleaned: String = input
        .chars()
        .map(|c| match c {
            '\n' | '\r' | '\t' => ' ',
            c if c.is_control() => ' ',
            c => c,
        })
        .collect();
    // Collapse repeated whitespace
    let parts: Vec<&str> = cleaned.split_whitespace().collect();
    cleaned = parts.join(" ");

    for sensitive_key in [
        "GsIdmsToken",
        "GSIdmsToken",
        "com.apple.gs.idms.pet",
        "X-Apple-Identity-Token",
        "HostPrivateKey",
        "RootPrivateKey",
        "BEGIN PRIVATE KEY",
        "BEGIN RSA PRIVATE KEY",
    ] {
        if cleaned.contains(sensitive_key) {
            return "Ошибка безопасности или авторизации (подробности скрыты).".to_string();
        }
    }

    const MAX_CHARS: usize = 360;
    if cleaned.chars().count() > MAX_CHARS {
        let truncated: String = cleaned.chars().take(MAX_CHARS).collect();
        format!("{truncated}...")
    } else {
        cleaned
    }
}

pub fn print_json_value<T: Serialize>(value: &T) {
    if let Ok(text) = serde_json::to_string_pretty(value) {
        let mut stdout = io::stdout().lock();
        let _ = writeln!(stdout, "{text}");
        let _ = stdout.flush();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn email_masking_hides_local_part() {
        assert_eq!(mask_email("alice@icloud.com"), "a***@icloud.com");
        assert_eq!(mask_email("x@example.org"), "*@example.org");
        assert_eq!(mask_email("  bob.smith@me.com "), "b***@me.com");
        assert_eq!(mask_email("not-an-email"), "***");
        assert!(!mask_email("secret.user@icloud.com").contains("secret"));
    }

    #[test]
    fn sanitize_redacts_sensitive_markers() {
        let leaked = "failed: GsIdmsToken=SUPERSECRET12345";
        let out = sanitize_message(leaked);
        assert!(!out.contains("SUPERSECRET12345"));
        assert!(!out.contains("GsIdmsToken"));

        let key_leak = "-----BEGIN PRIVATE KEY-----\nABCDEF";
        assert!(!sanitize_message(key_leak).contains("ABCDEF"));
    }

    #[test]
    fn stream_events_serialize_matching_contract() {
        let prog = StreamEvent::Progress {
            stage: "signing".into(),
            percent: 42,
            message: "Подпись приложения...".into(),
        };
        let v = serde_json::to_value(&prog).unwrap();
        assert_eq!(v["event"], "progress");
        assert_eq!(v["stage"], "signing");
        assert_eq!(v["percent"], 42);

        let twofa = StreamEvent::Need2fa {
            sms: false,
            retry: true,
            last_error: Some("Неверный код".into()),
            message: "Введите код".into(),
        };
        let v2 = serde_json::to_value(&twofa).unwrap();
        assert_eq!(v2["event"], "need_2fa");
        assert_eq!(v2["retry"], true);

        let done = StreamEvent::Done {
            ok: true,
            email_masked: None,
            bundle_id: Some("com.SideStore.SideStore.TEAM123456".into()),
            app_name: Some("SideStore".into()),
            udid: Some("00000000-0000AABBCCDDEEFF".into()),
            special_app: Some("SideStore".into()),
            team_id: None,
            expires_at_unix: None,
            hours_remaining: None,
            saved_path: None,
            remote_path: None,
            synced_session: None,
            message: "Готово".into(),
        };
        let v3 = serde_json::to_value(&done).unwrap();
        assert_eq!(v3["event"], "done");
        assert_eq!(v3["ok"], true);
        assert_eq!(v3["bundle_id"], "com.SideStore.SideStore.TEAM123456");
        assert!(v3.get("team_id").is_none());
    }
}
