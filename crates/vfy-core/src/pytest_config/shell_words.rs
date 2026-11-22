//! POSIX shell word splitting for pytest argument strings.

#[derive(Clone, Copy)]
enum ShellQuote {
    Single,
    Double,
}

/// Split pytest INI argument strings using POSIX shell quoting.
///
/// # Errors
/// Returns an error for an unterminated quotation or escape.
pub fn split_shell_words(value: &str) -> Result<Vec<String>, String> {
    let mut words = Vec::new();
    let mut word = String::new();
    let mut chars = value.chars().peekable();
    let mut quote: Option<ShellQuote> = None;
    let mut started = false;
    while let Some(character) = chars.next() {
        match quote {
            Some(ShellQuote::Single) => {
                if character == '\'' {
                    quote = None;
                } else {
                    word.push(character);
                }
            }
            Some(ShellQuote::Double) => match character {
                '"' => quote = None,
                '\\' if chars.peek().is_none() => return Err("No escaped character".to_owned()),
                '\\' if chars.peek().is_some_and(|next| matches!(next, '"' | '\\')) => {
                    if let Some(next) = chars.next() {
                        word.push(next);
                    }
                }
                _ => word.push(character),
            },
            None => match character {
                '\'' | '"' => {
                    quote = Some(if character == '\'' {
                        ShellQuote::Single
                    } else {
                        ShellQuote::Double
                    });
                    started = true;
                }
                '\\' => {
                    let Some(next) = chars.next() else {
                        return Err("No escaped character".to_owned());
                    };
                    word.push(next);
                    started = true;
                }
                ' ' | '\t' | '\r' | '\n' => {
                    if started {
                        words.push(std::mem::take(&mut word));
                        started = false;
                    }
                }
                _ => {
                    word.push(character);
                    started = true;
                }
            },
        }
    }
    if quote.is_some() {
        return Err("No closing quotation".to_owned());
    }
    if started {
        words.push(word);
    }
    Ok(words)
}
