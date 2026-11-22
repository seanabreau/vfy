//! Escaping shared by HTML report renderers.

use std::borrow::Cow;

pub(crate) fn escape(value: &str) -> Cow<'_, str> {
    let Some(first) = value
        .bytes()
        .position(|byte| matches!(byte, b'&' | b'<' | b'>' | b'"' | b'\''))
    else {
        return Cow::Borrowed(value);
    };
    let mut output = String::with_capacity(value.len());
    let mut start = 0;
    for (offset, byte) in value[first..].bytes().enumerate() {
        let replacement = match byte {
            b'&' => "&amp;",
            b'<' => "&lt;",
            b'>' => "&gt;",
            b'"' => "&quot;",
            b'\'' => "&#x27;",
            _ => continue,
        };
        // ASCII matches are UTF-8 boundaries, including beside multibyte characters.
        let index = first + offset;
        output.push_str(&value[start..index]);
        output.push_str(replacement);
        start = index + 1;
    }
    output.push_str(&value[start..]);
    Cow::Owned(output)
}

#[cfg(test)]
mod tests;
