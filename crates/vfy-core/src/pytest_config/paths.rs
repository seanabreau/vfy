//! Lexical path handling and environment, home-directory, and variable expansion.

use std::env;
use std::path::{Component, Path, PathBuf};

use super::{ConfigResult, PytestConfigError};

pub(super) fn target_directories(targets: &[String], invocation_dir: &Path) -> Vec<PathBuf> {
    targets
        .iter()
        .filter_map(|target| {
            let path_part = target
                .split_once("::")
                .map_or(target.as_str(), |(path, _)| path);
            let path = absolute_path(invocation_dir, Path::new(path_part));
            if !path.exists() {
                return None;
            }
            Some(if path.is_dir() {
                path
            } else {
                path.parent()?.to_path_buf()
            })
        })
        .collect()
}

pub(super) fn common_ancestor(
    paths: impl IntoIterator<Item = PathBuf>,
    invocation_dir: &Path,
) -> PathBuf {
    let mut ancestor: Option<PathBuf> = None;
    for path in paths {
        if !path.exists() {
            continue;
        }
        ancestor = match ancestor {
            None => Some(path),
            Some(current) if current == path || current.starts_with(&path) => Some(path),
            Some(current) if path.starts_with(&current) => Some(current),
            Some(current) => Some(
                shared_ancestor(&current, &path).unwrap_or_else(|| invocation_dir.to_path_buf()),
            ),
        };
    }
    let Some(mut ancestor) = ancestor else {
        return invocation_dir.to_path_buf();
    };
    if ancestor.is_file() {
        ancestor.pop();
    }
    ancestor
}

fn shared_ancestor(left: &Path, right: &Path) -> Option<PathBuf> {
    let mut left_components = left.components().peekable();
    let mut right_components = right.components().peekable();
    let mut common = PathBuf::new();
    loop {
        match (left_components.peek(), right_components.peek()) {
            (Some(left), Some(right)) if left == right => {
                common.push(left.as_os_str());
                left_components.next();
                right_components.next();
            }
            _ => break,
        }
    }
    (!common.as_os_str().is_empty()).then_some(common)
}

pub(super) fn absolute_path(base: &Path, path: &Path) -> PathBuf {
    let absolute = if path.is_absolute() {
        path.to_path_buf()
    } else {
        base.join(path)
    };
    lexical_normalize(&absolute)
}

fn lexical_normalize(path: &Path) -> PathBuf {
    let mut normalized = PathBuf::new();
    for component in path.components() {
        match component {
            Component::CurDir => {}
            Component::ParentDir => {
                normalized.pop();
            }
            other => normalized.push(other.as_os_str()),
        }
    }
    normalized
}

pub(super) fn expand_vars(value: &str) -> String {
    #[cfg(windows)]
    let value = expand_percent_vars(value);
    #[cfg(windows)]
    let value = value.as_str();
    expand_dollar_vars(value)
}

pub(super) fn expand_dollar_vars(value: &str) -> String {
    let mut output = String::new();
    let mut chars = value.chars().peekable();
    while let Some(character) = chars.next() {
        if character != '$' {
            output.push(character);
            continue;
        }
        let (name, braced, closed) = if chars.peek() == Some(&'{') {
            chars.next();
            let mut name = String::new();
            let mut closed = false;
            for next in chars.by_ref() {
                if next == '}' {
                    closed = true;
                    break;
                }
                name.push(next);
            }
            (name, true, closed)
        } else {
            let mut name = String::new();
            while chars
                .peek()
                .is_some_and(|next| next.is_ascii_alphanumeric() || *next == '_')
            {
                let Some(next) = chars.next() else { break };
                name.push(next);
            }
            (name, false, true)
        };
        if braced && !closed {
            output.push_str("${");
            output.push_str(&name);
            continue;
        }
        if braced && name.is_empty() {
            output.push_str("${}");
            continue;
        }
        if name.is_empty() {
            output.push('$');
        } else if let Some(value) = env::var_os(&name) {
            output.push_str(&value.to_string_lossy());
        } else {
            output.push('$');
            if braced {
                output.push('{');
            }
            output.push_str(&name);
            if braced {
                output.push('}');
            }
        }
    }
    output
}

pub(super) fn expand_home(value: &str) -> ConfigResult<String> {
    if !value.starts_with('~') {
        return Ok(value.to_owned());
    }
    #[cfg(windows)]
    let username_end = value.find(['/', '\\']).unwrap_or(value.len());
    #[cfg(not(windows))]
    let username_end = value.find('/').unwrap_or(value.len());
    let username = &value[1..username_end];
    let Some(home) = home_directory((!username.is_empty()).then_some(username))? else {
        return Ok(value.to_owned());
    };
    let home = home.to_string_lossy();
    #[cfg(not(windows))]
    let home = home.trim_end_matches('/');
    let expanded = format!("{home}{}", &value[username_end..]);
    #[cfg(not(windows))]
    if expanded.is_empty() {
        return Ok("/".to_owned());
    }
    Ok(expanded)
}

#[cfg(windows)]
fn home_directory(username: Option<&str>) -> ConfigResult<Option<PathBuf>> {
    let home = env::var_os("USERPROFILE").map(PathBuf::from).or_else(|| {
        let drive = env::var_os("HOMEDRIVE").unwrap_or_default();
        let path = env::var_os("HOMEPATH")?;
        Some(PathBuf::from(drive).join(path))
    });
    let Some(home) = home else { return Ok(None) };
    let Some(username) = username else {
        return Ok(Some(home));
    };
    let current_username = env::var("USERNAME").ok();
    if current_username.as_deref() == Some(username) {
        return Ok(Some(home));
    }
    let home_text = home.to_string_lossy();
    if current_username.as_deref() != home_text.rsplit(['/', '\\']).next() {
        return Ok(None);
    }
    Ok(home.parent().map(|parent| parent.join(username)))
}

#[cfg(not(windows))]
fn home_directory(username: Option<&str>) -> ConfigResult<Option<PathBuf>> {
    if username.is_none()
        && let Some(home) = env::var_os("HOME")
    {
        return Ok(Some(PathBuf::from(home)));
    }
    // pytest uses the account database for ~user and when HOME is absent.
    let user = match username {
        Some(username) => nix::unistd::User::from_name(username),
        None => nix::unistd::User::from_uid(nix::unistd::Uid::current()),
    }
    .map_err(|error| {
        PytestConfigError::message(format!("failed to resolve user home directory: {error}"))
    })?;
    Ok(user.map(|user| user.dir))
}

#[cfg(windows)]
fn expand_percent_vars(value: &str) -> String {
    let mut output = String::new();
    let mut chars = value.chars().peekable();
    while let Some(character) = chars.next() {
        if character != '%' {
            output.push(character);
            continue;
        }
        let mut name = String::new();
        let mut closed = false;
        for next in chars.by_ref() {
            if next == '%' {
                closed = true;
                break;
            }
            name.push(next);
        }
        if closed && let Some(value) = env::var_os(&name) {
            output.push_str(&value.to_string_lossy());
        } else if closed {
            output.push('%');
            output.push_str(&name);
            output.push('%');
        } else {
            output.push('%');
            output.push_str(&name);
        }
    }
    output
}
