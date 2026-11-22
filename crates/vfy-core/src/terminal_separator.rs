//! Shared separator layout for runner and coverage reports.

// A terminal width is untrusted input; reserve bounded output before rendering.
const MAX_TERMINAL_BYTES: usize = 16 * 1024 * 1024;

/// Allocation and argument failures while formatting a terminal separator.
#[derive(Debug, Clone, Copy)]
pub enum SeparatorError {
    Empty,
    TooLarge,
    Memory,
}

fn python_whitespace(character: char) -> bool {
    character.is_whitespace() || matches!(character, '\u{1c}'..='\u{1f}')
}

/// Format a terminal separator within the requested display width.
///
/// # Errors
/// Returns an error for an empty separator, an overflow, or an excessive allocation.
pub fn separator(sep: &str, title: Option<&str>, width: i64) -> Result<String, SeparatorError> {
    let sep_len = sep.chars().count();
    if sep_len == 0 {
        return Err(SeparatorError::Empty);
    }
    let width = i128::from(width);
    let title_len = title.map_or(0, |title| title.chars().count());
    let repetitions = if title.is_some() {
        ((width - title_len as i128 - 2) / (2 * sep_len as i128)).max(1)
    } else {
        (width / sep_len as i128).max(0)
    };
    let repetitions = usize::try_from(repetitions).map_err(|_| SeparatorError::TooLarge)?;
    let sides = if title.is_some() { 2 } else { 1 };
    // Sums the repeated separator units and the title frame, failing on overflow.
    let measure = |unit: usize, frame: usize| {
        unit.checked_mul(repetitions)
            .and_then(|count| count.checked_mul(sides))
            .and_then(|count| count.checked_add(frame))
            .ok_or(SeparatorError::TooLarge)
    };
    let characters = measure(sep_len, title.map_or(0, |_| title_len + 2))?;
    let trimmed = sep.trim_end_matches(python_whitespace);
    let suffix = if characters as i128 + trimmed.chars().count() as i128 <= width {
        trimmed
    } else {
        ""
    };
    let capacity = measure(sep.len(), title.map_or(0, |title| title.len() + 2))?
        .checked_add(suffix.len())
        .ok_or(SeparatorError::TooLarge)?;
    if capacity > MAX_TERMINAL_BYTES {
        return Err(SeparatorError::Memory);
    }
    let mut rendered = String::new();
    rendered
        .try_reserve_exact(capacity)
        .map_err(|_| SeparatorError::Memory)?;
    for _ in 0..repetitions {
        rendered.push_str(sep);
    }
    if let Some(title) = title {
        rendered.push(' ');
        rendered.push_str(title);
        rendered.push(' ');
        for _ in 0..repetitions {
            rendered.push_str(sep);
        }
    }
    rendered.push_str(suffix);
    Ok(rendered)
}
