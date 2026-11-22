//! String equality descriptions at the configured assertion verbosity.

pub(super) fn shorten(text: &str, limit: usize) -> String {
    let characters: Vec<char> = text.chars().collect();
    if characters.len() <= limit {
        return text.to_owned();
    }
    if limit <= 3 {
        return ".".repeat(limit);
    }
    let start_count = (limit - 3) / 2;
    let end_count = limit - 3 - start_count;
    let start: String = characters[..start_count].iter().collect();
    let end: String = characters[characters.len() - end_count..].iter().collect();
    format!("{start}...{end}")
}

fn lines(text: &str) -> Vec<String> {
    let mut output = Vec::new();
    let mut start = 0;
    let mut characters = text.char_indices().peekable();
    while let Some((offset, character)) = characters.next() {
        if matches!(
            character,
            '\n' | '\r'
                | '\u{000b}'
                | '\u{000c}'
                | '\u{001c}'
                | '\u{001d}'
                | '\u{001e}'
                | '\u{0085}'
                | '\u{2028}'
                | '\u{2029}'
        ) {
            let mut end = offset + character.len_utf8();
            if character == '\r' && characters.peek().is_some_and(|(_, next)| *next == '\n') {
                characters.next();
                end += 1;
            }
            output.push(text[start..end].to_owned());
            start = end;
        }
    }
    if start != text.len() {
        output.push(text[start..].to_owned());
    }
    output
}

fn display_line(line: &str) -> &str {
    line.trim_end_matches([
        '\n', '\r', '\u{000b}', '\u{000c}', '\u{001c}', '\u{001d}', '\u{001e}', '\u{0085}',
        '\u{2028}', '\u{2029}',
    ])
}

fn character_matches(old: &[char], new: &[char]) -> Option<(Vec<Vec<usize>>, usize)> {
    let cells = (old.len() + 1).checked_mul(new.len() + 1)?;
    if cells > 2_000_000 {
        return None;
    }
    let mut common = vec![vec![0; new.len() + 1]; old.len() + 1];
    for left in (0..old.len()).rev() {
        for right in (0..new.len()).rev() {
            common[left][right] = if old[left] == new[right] {
                1 + common[left + 1][right + 1]
            } else {
                common[left + 1][right].max(common[left][right + 1])
            };
        }
    }
    let count = common[0][0];
    Some((common, count))
}

fn markers(old: &[char], new: &[char], common: &[Vec<usize>]) -> (String, String) {
    let mut old_marks: Vec<char> = old
        .iter()
        .map(|character| if *character == '\t' { '\t' } else { ' ' })
        .collect();
    let mut new_marks: Vec<char> = new
        .iter()
        .map(|character| if *character == '\t' { '\t' } else { ' ' })
        .collect();
    let (mut left, mut right) = (0, 0);
    while left < old.len() || right < new.len() {
        if left < old.len() && right < new.len() && old[left] == new[right] {
            left += 1;
            right += 1;
            continue;
        }
        let left_start = left;
        let right_start = right;
        while left < old.len() || right < new.len() {
            if left < old.len() && right < new.len() && old[left] == new[right] {
                break;
            }
            if left < old.len()
                && (right == new.len() || common[left + 1][right] >= common[left][right + 1])
            {
                left += 1;
            } else {
                right += 1;
            }
        }
        old_marks[left_start..left].fill(if right == right_start { '-' } else { '^' });
        new_marks[right_start..right].fill(if left == left_start { '+' } else { '^' });
    }
    (
        old_marks.iter().collect::<String>().trim_end().to_owned(),
        new_marks.iter().collect::<String>().trim_end().to_owned(),
    )
}

fn replacement(expected: &str, actual: &str, output: &mut Vec<String>) {
    let old: Vec<char> = expected.chars().collect();
    let new: Vec<char> = actual.chars().collect();
    let marking = character_matches(&old, &new)
        .filter(|(_, count)| 8 * count >= 3 * (old.len() + new.len()))
        .map(|(common, _)| markers(&old, &new, &common));
    output.push(format!("- {}", display_line(expected)));
    if let Some((marker, _)) = &marking
        && !marker.is_empty()
    {
        output.push(format!("? {marker}"));
    }
    output.push(format!("+ {}", display_line(actual)));
    if let Some((_, marker)) = marking
        && !marker.is_empty()
    {
        output.push(format!("? {marker}"));
    }
}

fn line_diff(expected: &[String], actual: &[String]) -> Vec<String> {
    let width = actual.len() + 1;
    let cells = (expected.len() + 1).checked_mul(width);
    let Some(cells) = cells.filter(|cells| *cells <= 2_000_000) else {
        return expected
            .iter()
            .map(|line| format!("- {}", display_line(line)))
            .chain(
                actual
                    .iter()
                    .map(|line| format!("+ {}", display_line(line))),
            )
            .collect();
    };
    let mut common = vec![0usize; cells];
    for old in (0..expected.len()).rev() {
        for new in (0..actual.len()).rev() {
            common[old * width + new] = if expected[old] == actual[new] {
                1 + common[(old + 1) * width + new + 1]
            } else {
                common[(old + 1) * width + new].max(common[old * width + new + 1])
            };
        }
    }
    let (mut old, mut new) = (0, 0);
    let mut output = Vec::new();
    while old < expected.len() || new < actual.len() {
        if old < expected.len() && new < actual.len() && expected[old] == actual[new] {
            output.push(format!("  {}", display_line(&expected[old])));
            old += 1;
            new += 1;
            continue;
        }
        let old_start = old;
        let new_start = new;
        while old < expected.len() || new < actual.len() {
            if old < expected.len() && new < actual.len() && expected[old] == actual[new] {
                break;
            }
            if old < expected.len()
                && (new == actual.len()
                    || common[(old + 1) * width + new] >= common[old * width + new + 1])
            {
                old += 1;
            } else {
                new += 1;
            }
        }
        if old - old_start == 1 && new - new_start == 1 {
            replacement(&expected[old_start], &actual[new_start], &mut output);
        } else {
            let removed = expected[old_start..old]
                .iter()
                .map(|line| format!("- {}", display_line(line)));
            let added = actual[new_start..new]
                .iter()
                .map(|line| format!("+ {}", display_line(line)));
            // difflib's plain replacement emits the shorter side first.
            if new - new_start < old - old_start {
                output.extend(added);
                output.extend(removed);
            } else {
                output.extend(removed);
                output.extend(added);
            }
        }
    }
    output
}

pub(super) fn prepare_difference(
    left: &str,
    right: &str,
    verbosity: u32,
) -> super::TextDifferenceInput {
    let mut actual: Vec<char> = left.chars().collect();
    let mut expected: Vec<char> = right.chars().collect();
    let mut details = Vec::new();
    let prefix = actual
        .iter()
        .zip(&expected)
        .take_while(|(left, right)| left == right)
        .count()
        .min(actual.len().min(expected.len()).saturating_sub(1));
    if verbosity == 0 && prefix > 42 {
        let skipped = prefix - 10;
        actual.drain(..skipped);
        expected.drain(..skipped);
        details.push(format!(
            "Skipping {skipped} identical leading characters in diff, use -v to show"
        ));
    }
    // Pytest includes the first character in its reverse-context scan.
    let mut suffix = 0;
    while actual.len() == expected.len() && suffix < actual.len() {
        let old = if suffix == 0 {
            0
        } else {
            actual.len() - suffix
        };
        let new = if suffix == 0 {
            0
        } else {
            expected.len() - suffix
        };
        if actual[old] != expected[new] {
            break;
        }
        suffix += 1;
    }
    if verbosity == 0 && suffix > 42 {
        let skipped = suffix - 10;
        actual.truncate(actual.len() - skipped);
        expected.truncate(expected.len() - skipped);
        details.push(format!(
            "Skipping {skipped} identical trailing characters in diff, use -v to show"
        ));
    }
    let actual: String = actual.iter().collect();
    let expected: String = expected.iter().collect();
    let whitespace = |text: &str| {
        !text.is_empty()
            && text.chars().all(|character| {
                character.is_whitespace() || ('\x1c'..='\x1f').contains(&character)
            })
    };
    super::TextDifferenceInput {
        escape_whitespace: whitespace(&actual) || whitespace(&expected),
        left: actual,
        right: expected,
        notices: details,
    }
}

fn difference(left: &str, right: &str, verbosity: super::AssertionVerbosity) -> Vec<String> {
    let level = u32::from(!matches!(verbosity, super::AssertionVerbosity::Normal));
    let prepared = prepare_difference(left, right, level);
    let mut details = prepared.notices;
    details.extend(line_diff(&lines(&prepared.right), &lines(&prepared.left)));
    details
}

pub(super) fn equality(
    left: &str,
    right: &str,
    left_repr: &str,
    right_repr: &str,
    verbosity: super::AssertionVerbosity,
    truncation: super::AssertionTruncation,
) -> String {
    super::default_difference(
        format!(
            "{} == {}",
            verbosity.summary(left_repr),
            verbosity.summary(right_repr)
        ),
        difference(left, right, verbosity),
        verbosity,
        truncation,
    )
}

#[cfg(test)]
#[path = "strings/tests.rs"]
mod tests;
