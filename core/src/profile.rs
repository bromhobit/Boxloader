use apple_codesign_quick::ProvisioningProfile;
use std::time::{SystemTime, UNIX_EPOCH};

pub const MAX_PROFILE_BYTES: usize = 8 * 1024 * 1024;
pub const MAX_BUNDLE_ID_LEN: usize = 255;

pub fn validate_bundle_id(bundle: &str) -> Result<(), String> {
    if bundle.is_empty()
        || bundle.len() > MAX_BUNDLE_ID_LEN
        || !bundle.contains('.')
        || bundle
            .split('.')
            .any(|s| s.is_empty() || !s.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-'))
    {
        return Err(format!("некорректный bundle ID: {bundle}"));
    }
    Ok(())
}

pub fn parse_cms_profile_dict(bytes: &[u8]) -> Result<plist::Dictionary, String> {
    if bytes.is_empty() || bytes.len() > MAX_PROFILE_BYTES {
        return Err("недопустимый размер профиля подписи".into());
    }
    let parsed = ProvisioningProfile::parse(bytes)
        .map_err(|e| format!("не удалось разобрать CMS-профиль: {e}"))?;
    Ok(parsed.plist().clone())
}

pub fn profile_application_identifier(profile: &plist::Dictionary) -> Option<&str> {
    profile
        .get("Entitlements")
        .and_then(plist::Value::as_dictionary)
        .and_then(|e| e.get("application-identifier"))
        .and_then(plist::Value::as_string)
}

pub fn matches_bundle_id(app_id: &str, bundle: &str) -> bool {
    if app_id.contains('*') {
        return false;
    }
    app_id
        .split_once('.')
        .is_some_and(|(prefix, suffix)| !prefix.is_empty() && suffix == bundle)
}

pub fn profile_team_id(profile: &plist::Dictionary) -> Option<String> {
    profile
        .get("TeamIdentifier")
        .and_then(plist::Value::as_array)
        .and_then(|arr| arr.iter().find_map(plist::Value::as_string))
        .map(ToOwned::to_owned)
        .or_else(|| {
            profile_application_identifier(profile)
                .and_then(|id| id.split_once('.'))
                .map(|(prefix, _)| prefix.to_string())
                .filter(|p| !p.is_empty())
        })
}

pub fn profile_expiration_unix(profile: &plist::Dictionary) -> Result<u64, String> {
    let date = profile
        .get("ExpirationDate")
        .and_then(plist::Value::as_date)
        .ok_or("в профиле отсутствует дата истечения ExpirationDate")?;
    SystemTime::from(date)
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .map_err(|_| "некорректная дата истечения профиля".into())
}

pub fn profile_creation_unix(profile: &plist::Dictionary) -> u64 {
    profile
        .get("CreationDate")
        .and_then(plist::Value::as_date)
        .and_then(|d| SystemTime::from(d).duration_since(UNIX_EPOCH).ok())
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

fn non_empty_array<'a>(
    profile: &'a plist::Dictionary,
    key: &str,
) -> Result<&'a [plist::Value], String> {
    profile
        .get(key)
        .and_then(plist::Value::as_array)
        .map(Vec::as_slice)
        .filter(|arr| !arr.is_empty())
        .ok_or_else(|| format!("в профиле отсутствует поле {key}"))
}

pub fn find_newest_matching_profile<'a>(
    profiles: &'a [plist::Dictionary],
    bundle: &str,
    team_filter: Option<&str>,
) -> Option<&'a plist::Dictionary> {
    profiles
        .iter()
        .filter(|p| {
            let Some(app_id) = profile_application_identifier(p) else {
                return false;
            };
            if !matches_bundle_id(app_id, bundle) {
                return false;
            }
            if let Some(team) = team_filter {
                if !p
                    .get("TeamIdentifier")
                    .and_then(plist::Value::as_array)
                    .is_some_and(|arr| arr.iter().any(|x| x.as_string() == Some(team)))
                {
                    return false;
                }
            }
            profile_expiration_unix(p).is_ok()
        })
        .max_by_key(|p| {
            (
                profile_expiration_unix(p).unwrap_or(0),
                profile_creation_unix(p),
            )
        })
}

pub fn check_replacement_compatible(
    old: &plist::Dictionary,
    new: &plist::Dictionary,
    team: &str,
    bundle: &str,
    device_udid: &str,
    now_unix: u64,
) -> Result<u64, String> {
    fn entitlements(p: &plist::Dictionary) -> Result<&plist::Dictionary, String> {
        p.get("Entitlements")
            .and_then(plist::Value::as_dictionary)
            .ok_or_else(|| "в профиле отсутствуют Entitlements".to_string())
    }

    let old_ent = entitlements(old)?;
    let new_ent = entitlements(new)?;
    let app = old_ent
        .get("application-identifier")
        .and_then(plist::Value::as_string)
        .ok_or("в профиле отсутствует application-identifier")?;
    if !matches_bundle_id(app, bundle) || old_ent != new_ent {
        return Err("application-identifier или entitlements нового профиля не совпадают".into());
    }

    let old_teams = non_empty_array(old, "TeamIdentifier")?;
    let new_teams = non_empty_array(new, "TeamIdentifier")?;
    if old_teams != new_teams || !old_teams.iter().any(|x| x.as_string() == Some(team)) {
        return Err("TeamIdentifier нового профиля не совпадает с установленным".into());
    }

    for p in [old, new] {
        if !non_empty_array(p, "ProvisionedDevices")?.iter().any(|x| {
            x.as_string()
                .is_some_and(|id| id.eq_ignore_ascii_case(device_udid))
        }) {
            return Err("профиль не включает UDID подключённого iPhone".into());
        }
    }

    let new_certs = non_empty_array(new, "DeveloperCertificates")?;
    if !non_empty_array(old, "DeveloperCertificates")?
        .iter()
        .all(|c| c.as_data().is_some() && new_certs.contains(c))
    {
        return Err("сертификат подписи в новом профиле отличается".into());
    }

    let old_exp = profile_expiration_unix(old)?;
    let new_exp = profile_expiration_unix(new)?;
    if new_exp <= old_exp || new_exp <= now_unix {
        return Err("Apple не продлила срок действия профиля".into());
    }
    Ok(new_exp)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    fn sample_profile(
        team: &str,
        bundle: &str,
        udid: &str,
        cert: &[u8],
        created: u64,
        expires: u64,
    ) -> plist::Dictionary {
        let mut ent = plist::Dictionary::new();
        ent.insert(
            "application-identifier".into(),
            plist::Value::String(format!("{team}.{bundle}")),
        );
        let mut d = plist::Dictionary::new();
        d.insert("Entitlements".into(), plist::Value::Dictionary(ent));
        d.insert(
            "TeamIdentifier".into(),
            plist::Value::Array(vec![plist::Value::String(team.into())]),
        );
        d.insert(
            "ProvisionedDevices".into(),
            plist::Value::Array(vec![plist::Value::String(udid.into())]),
        );
        d.insert(
            "DeveloperCertificates".into(),
            plist::Value::Array(vec![plist::Value::Data(cert.to_vec())]),
        );
        d.insert(
            "CreationDate".into(),
            plist::Value::Date((UNIX_EPOCH + Duration::from_secs(created)).into()),
        );
        d.insert(
            "ExpirationDate".into(),
            plist::Value::Date((UNIX_EPOCH + Duration::from_secs(expires)).into()),
        );
        d
    }

    #[test]
    fn validates_bundle_id_strictly() {
        assert!(validate_bundle_id("com.SideStore.SideStore.TEAM123456").is_ok());
        assert!(validate_bundle_id("org.my-app.test").is_ok());
        assert!(validate_bundle_id("").is_err());
        assert!(validate_bundle_id("nodot").is_err());
        assert!(validate_bundle_id("com.wild.*").is_err());
        assert!(validate_bundle_id("com.bad/path").is_err());
    }

    #[test]
    fn finds_newest_matching_profile_and_ignores_wildcards() {
        let b = "com.SideStore.SideStore.TEAM123456";
        let old = sample_profile("TEAM123456", b, "UDID-1", b"C1", 100, 1000);
        let wild = sample_profile("TEAM123456", "*", "UDID-1", b"C1", 500, 9999);
        let fresh = sample_profile("TEAM123456", b, "UDID-1", b"C1", 200, 2000);
        let list = vec![old, wild, fresh];
        let best = find_newest_matching_profile(&list, b, Some("TEAM123456")).unwrap();
        assert_eq!(profile_expiration_unix(best).unwrap(), 2000);
        assert_eq!(profile_team_id(best).as_deref(), Some("TEAM123456"));
    }

    #[test]
    fn replacement_compatibility_enforces_security_invariants() {
        let now = 1_760_000_000;
        let b = "com.SideStore.SideStore.TEAM123456";
        let old = sample_profile("TEAM123456", b, "00000000-ABC", b"CERT1", now - 100, now + 3600);
        let good = sample_profile(
            "TEAM123456",
            b,
            "00000000-abc",
            b"CERT1",
            now,
            now + 7 * 86400,
        );
        assert_eq!(
            check_replacement_compatible(&old, &good, "TEAM123456", b, "00000000-ABC", now)
                .unwrap(),
            now + 7 * 86400
        );

        let wrong_cert =
            sample_profile("TEAM123456", b, "00000000-ABC", b"CERT2", now, now + 7 * 86400);
        assert!(
            check_replacement_compatible(&old, &wrong_cert, "TEAM123456", b, "00000000-ABC", now)
                .is_err()
        );

        let not_extended =
            sample_profile("TEAM123456", b, "00000000-ABC", b"CERT1", now, now + 3600);
        assert!(
            check_replacement_compatible(
                &old,
                &not_extended,
                "TEAM123456",
                b,
                "00000000-ABC",
                now
            )
            .is_err()
        );
    }
}
