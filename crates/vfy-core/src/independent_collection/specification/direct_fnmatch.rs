//! Direct Python fnmatch evaluation for live collection settings.

/// Match one Unicode string without constructing a regular expression.
pub(super) fn matches(pattern: &str, value: &str) -> bool {
    let pattern: Vec<char> = pattern.chars().collect();
    let value: Vec<char> = value.chars().collect();
    let (mut pattern_index, mut value_index) = (0, 0);
    let mut star = None;

    while value_index < value.len() {
        let next = match pattern.get(pattern_index).copied() {
            Some('*') => {
                pattern_index += 1;
                star = Some((pattern_index, value_index));
                continue;
            }
            Some('?') => Some((true, pattern_index + 1)),
            Some('[') => Some(character_class(&pattern, pattern_index, value[value_index])),
            Some(character) => Some((character == value[value_index], pattern_index + 1)),
            None => None,
        };
        if let Some((true, next_index)) = next {
            pattern_index = next_index;
            value_index += 1;
        } else if let Some((next_index, consumed)) = star {
            let consumed = consumed + 1;
            star = Some((next_index, consumed));
            pattern_index = next_index;
            value_index = consumed;
        } else {
            return false;
        }
    }
    pattern[pattern_index..]
        .iter()
        .all(|character| *character == '*')
}

/// Return the match and next pattern offset, preserving Python's malformed ranges.
fn character_class(pattern: &[char], start: usize, value: char) -> (bool, usize) {
    let content = start + 1;
    let mut end = content;
    if pattern.get(end) == Some(&'!') {
        end += 1;
    }
    if pattern.get(end) == Some(&']') {
        end += 1;
    }
    while pattern.get(end).is_some_and(|character| *character != ']') {
        end += 1;
    }
    if end == pattern.len() {
        return (value == '[', content);
    }

    // CPython removes descending ranges before interpreting a leading `!`.
    // Keep the chunks separate so literal hyphens stay distinct from ranges.
    let mut chunks: Vec<Vec<char>> = Vec::new();
    let mut segment = content;
    let mut search = content
        + if pattern.get(content) == Some(&'!') {
            2
        } else {
            1
        };
    while search < end {
        let Some(relative) = pattern[search..end]
            .iter()
            .position(|character| *character == '-')
        else {
            break;
        };
        let separator = search + relative;
        chunks.push(pattern[segment..separator].to_vec());
        segment = separator + 1;
        search = separator + 3;
    }
    if chunks.is_empty() {
        chunks.push(pattern[content..end].to_vec());
    } else if segment < end {
        chunks.push(pattern[segment..end].to_vec());
    } else if let Some(last) = chunks.last_mut() {
        last.push('-');
    }
    for index in (1..chunks.len()).rev() {
        let descending = chunks[index - 1]
            .last()
            .zip(chunks[index].first())
            .is_some_and(|(first, last)| first > last);
        if descending {
            let mut right = chunks.remove(index);
            chunks[index - 1].pop();
            right.remove(0);
            chunks[index - 1].extend(right);
        }
    }
    let negated = chunks.first().and_then(|chunk| chunk.first()) == Some(&'!');
    if negated {
        chunks[0].remove(0);
    }
    let mut found = chunks.iter().any(|chunk| chunk.contains(&value));
    for pair in chunks.windows(2) {
        match (pair[0].last(), pair[1].first()) {
            (Some(first), Some(last)) => found |= *first <= value && value <= *last,
            _ => found |= value == '-',
        }
    }
    (found != negated, end + 1)
}

#[cfg(test)]
mod tests;
