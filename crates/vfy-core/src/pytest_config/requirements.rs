//! The requirement syntax used by pytest's loaded-plugin check.
//!
//! Extras and environment markers must be well formed, but pytest deliberately
//! does not evaluate them. URLs remain opaque and names keep their original case.
use super::python_whitespace;
use pep440_rs::{Version, VersionSpecifier, VersionSpecifiers};

#[derive(Debug)]
pub(super) struct PluginRequirement<'a> {
    pub(super) name: &'a str,
    specifiers: VersionSpecifiers,
    arbitrary: Vec<&'a str>,
}

impl<'a> PluginRequirement<'a> {
    pub(super) fn parse(source: &'a str) -> Option<Self> {
        let mut cursor = Cursor(source);
        cursor.spaces();
        let name = cursor.identifier()?;
        cursor.spaces();
        if cursor.take("[") {
            cursor.spaces();
            if !cursor.take("]") {
                loop {
                    cursor.identifier()?;
                    cursor.spaces();
                    if cursor.take("]") {
                        break;
                    }
                    cursor.require(",")?;
                    cursor.spaces();
                }
            }
            cursor.spaces();
        }
        let mut specifiers = Vec::new();
        let mut arbitrary = Vec::new();
        if cursor.take("@") {
            cursor.spaces();
            let length = cursor.0.find([' ', '\t']).unwrap_or(cursor.0.len());
            if length == 0 {
                return None;
            }
            cursor.0 = &cursor.0[length..];
        } else {
            let parenthesized = cursor.take("(");
            cursor.spaces();
            while cursor.starts_operator() {
                let token = cursor.specifier()?;
                // Arbitrary equality consumes commas as part of its lexical
                // token; the resulting specifier set still separates clauses.
                for part in token.split(',').filter(|part| !part.is_empty()) {
                    if let Some(value) = part.strip_prefix("===") {
                        arbitrary.push(value.trim_matches(python_whitespace));
                    } else {
                        let version = part.trim_start_matches(['=', '!', '~', '<', '>']);
                        let operator = &part[..part.len() - version.len()];
                        let normalized = format!(
                            "{operator}{}",
                            version.trim_start_matches(python_whitespace)
                        );
                        specifiers.push(normalized.parse::<VersionSpecifier>().ok()?);
                    }
                }
                cursor.spaces();
                if !cursor.take(",") {
                    break;
                }
                cursor.spaces();
            }
            if parenthesized {
                cursor.require(")")?;
            }
        }
        cursor.spaces();
        if cursor.take(";") {
            cursor.markers()?;
        }
        cursor.spaces();
        cursor.0.is_empty().then(|| Self {
            name,
            specifiers: specifiers.into_iter().collect(),
            arbitrary,
        })
    }

    pub(super) fn contains(&self, version: &Version) -> bool {
        self.specifiers.contains(version)
            && self
                .arbitrary
                .iter()
                .all(|value| value.eq_ignore_ascii_case(&version.to_string()))
    }
}

struct Cursor<'a>(&'a str);

impl<'a> Cursor<'a> {
    fn spaces(&mut self) {
        self.0 = self.0.trim_start_matches([' ', '\t']);
    }

    fn take(&mut self, token: &str) -> bool {
        if let Some(rest) = self.0.strip_prefix(token) {
            self.0 = rest;
            true
        } else {
            false
        }
    }

    fn require(&mut self, token: &str) -> Option<()> {
        self.take(token).then_some(())
    }

    fn word(&mut self, word: &str) -> bool {
        if self.0.strip_prefix(word).is_some_and(|rest| {
            !rest.starts_with(|character: char| character.is_alphanumeric() || character == '_')
        }) {
            self.take(word)
        } else {
            false
        }
    }

    fn identifier(&mut self) -> Option<&'a str> {
        if !self
            .0
            .starts_with(|character: char| character.is_ascii_alphanumeric())
        {
            return None;
        }
        let length = self
            .0
            .find(|character: char| {
                !character.is_ascii_alphanumeric() && !matches!(character, '.' | '-' | '_')
            })
            .unwrap_or(self.0.len());
        let name = self.0[..length].trim_end_matches(['.', '-']);
        if self.0[name.len()..]
            .starts_with(|character: char| character.is_alphanumeric() || character == '_')
        {
            return None;
        }
        self.0 = &self.0[name.len()..];
        Some(name)
    }

    fn starts_operator(&self) -> bool {
        self.0.starts_with(['=', '!', '~', '<', '>'])
    }

    fn operator(&mut self) -> Option<()> {
        ["===", "==", "!=", "~=", "<=", ">=", "<", ">"]
            .into_iter()
            .any(|operator| self.take(operator))
            .then_some(())
    }

    fn specifier(&mut self) -> Option<&'a str> {
        let source = self.0;
        if self.take("===") {
            self.0 = self.0.trim_start_matches(python_whitespace);
            let length = self
                .0
                .find(|character: char| {
                    python_whitespace(character) || matches!(character, ';' | ')')
                })
                .unwrap_or(self.0.len());
            self.0 = &self.0[length..];
        } else {
            self.operator()?;
            self.0 = self.0.trim_start_matches(python_whitespace);
            let length = self
                .0
                .find(|character: char| {
                    !character.is_ascii_alphanumeric()
                        && !matches!(character, '.' | '_' | '-' | '+' | '!' | '*')
                })
                .unwrap_or(self.0.len());
            if length == 0 {
                return None;
            }
            self.0 = &self.0[length..];
        }
        Some(&source[..source.len() - self.0.len()])
    }

    fn operand(&mut self) -> Option<()> {
        self.spaces();
        if self.0.starts_with(['\'', '"']) {
            let quote = self.0.chars().next()?;
            let length = self.0[1..].find(quote)? + 2;
            let literal = &self.0[..length];
            ruff_python_parser::parse_expression(literal).ok()?;
            self.0 = &self.0[length..];
            return Some(());
        }
        [
            "python_version",
            "python_full_version",
            "os.name",
            "os_name",
            "sys.platform",
            "sys_platform",
            "platform_release",
            "platform_system",
            "platform.version",
            "platform_version",
            "platform.machine",
            "platform_machine",
            "platform.python_implementation",
            "platform_python_implementation",
            "python_implementation",
            "implementation_name",
            "implementation_version",
            "dependency_groups",
            "extras",
            "extra",
        ]
        .into_iter()
        .any(|name| self.word(name))
        .then_some(())
    }

    fn markers(&mut self) -> Option<()> {
        let mut depth = 0_usize;
        let mut needs_atom = true;
        loop {
            self.spaces();
            if needs_atom {
                if self.take("(") {
                    depth += 1;
                    continue;
                }
                self.operand()?;
                self.spaces();
                if self.word("not") {
                    self.spaces();
                    if !self.word("in") {
                        return None;
                    }
                } else if !self.word("in") {
                    self.operator()?;
                }
                self.operand()?;
                needs_atom = false;
            } else if self.word("and") || self.word("or") {
                needs_atom = true;
            } else if depth > 0 && self.take(")") {
                depth -= 1;
            } else {
                return (depth == 0).then_some(());
            }
        }
    }
}

#[cfg(test)]
mod tests;
