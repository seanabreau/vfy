//! Validate mutation input at a fresh interpreter's import boundary.

use super::{MutationIdentity, MutationInput, MutationPhase, source_hash};
use std::collections::{BTreeMap, BTreeSet};

/// Whether a function body has no execution outside recorded test contexts.
///
/// Import-time calls can populate globals consumed by tests that never call the
/// function themselves. Those mutations need a fresh, complete test run.
///
/// # Errors
/// Returns an error if the source line count overflows.
pub fn mutation_body_isolated(
    input: &MutationInput,
    source: &[u8],
    unattributed: &BTreeMap<String, BTreeSet<i32>>,
) -> Result<bool, String> {
    if input.owner.phase != MutationPhase::Body {
        return Ok(false);
    }
    let start = input.owner.start as usize;
    let end = input.owner.end as usize;
    let Some(body_start) = input.owner.body_start.map(|offset| offset as usize) else {
        return Ok(false);
    };
    if start >= end
        || end > source.len()
        || body_start < start
        || body_start >= end
        || body_start > input.start as usize
    {
        return Ok(false);
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;

        // Source filtering can omit import-time execution through another hardlink.
        // Its absence from measured paths cannot establish body isolation.
        if !std::fs::metadata(&input.path).is_ok_and(|metadata| metadata.nlink() == 1) {
            return Ok(false);
        }
    }
    let line = |offset| {
        i32::try_from(source[..offset].split(|byte| *byte == b'\n').count())
            .map_err(|_| "mutation source exceeds the supported line count".to_owned())
    };
    let first = line(start)?;
    let body = line(body_start)?;
    let last = line(end)?;
    Ok(first < body
        && unattributed
            .get(&input.path)
            .is_some_and(|lines| !lines.range(body..=last).any(|_| true)))
}

/// A mutation input validated against its catalog record.
#[derive(Clone)]
pub struct FreshMutation {
    input: MutationInput,
}

impl FreshMutation {
    /// Validate the catalog identity before issuing a fresh mutation execution.
    ///
    /// # Errors
    /// Rejects malformed source identities, byte ranges, or mutation keys.
    pub fn new(input: MutationInput) -> Result<Self, String> {
        if !std::path::Path::new(&input.path).is_absolute()
            || input.start >= input.end
            || input.source_hash.len() != 64
            || !input
                .source_hash
                .bytes()
                .all(|byte| byte.is_ascii_hexdigit())
            || input.owner.end < input.owner.start
        {
            return Err("invalid fresh mutation input".to_owned());
        }
        let identity = MutationIdentity {
            path: &input.path,
            digest: &input.source_hash,
            kind: &input.kind,
            operator: &input.operator,
            start: input.start,
            end: input.end,
            replacement: &input.replacement,
        };
        if identity.key() != input.id {
            return Err("fresh mutation identity disagrees with its catalog input".to_owned());
        }
        Ok(Self { input })
    }

    /// Borrow the validated catalog record.
    #[must_use]
    pub const fn input(&self) -> &MutationInput {
        &self.input
    }

    /// Apply the validated byte replacement before the target module is executed.
    ///
    /// # Errors
    /// Rejects changed source bytes or ranges before returning a replacement.
    pub fn patch(&self, source: &[u8]) -> Result<Vec<u8>, String> {
        let text = std::str::from_utf8(source)
            .map_err(|_| "fresh mutation source is not UTF-8".to_owned())?;
        if source_hash(text) != self.input.source_hash {
            return Err(format!(
                "mutation source digest changed for {}",
                self.input.path
            ));
        }
        let start = self.input.start as usize;
        let end = self.input.end as usize;
        if source.get(start..end) != Some(self.input.original.as_bytes()) {
            return Err(format!(
                "mutation source range changed for {}",
                self.input.path
            ));
        }
        let replacement = self.input.replacement.as_bytes();
        let length = source
            .len()
            .checked_sub(end - start)
            .and_then(|length| length.checked_add(replacement.len()))
            .ok_or_else(|| "fresh mutation replacement size overflowed".to_owned())?;
        let mut patched = Vec::with_capacity(length);
        patched.extend_from_slice(&source[..start]);
        patched.extend_from_slice(replacement);
        patched.extend_from_slice(&source[end..]);
        Ok(patched)
    }
}

#[cfg(test)]
mod tests;
