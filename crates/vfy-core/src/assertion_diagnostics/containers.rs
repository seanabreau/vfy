//! Default-verbosity differences for container comparisons.

use super::strings::shorten;

pub enum ContainerComparison {
    Sequence {
        left: String,
        right: String,
        difference: Option<IndexDifference>,
        extra: Option<ExtraItems>,
    },
    Dictionary {
        left: String,
        right: String,
        details: Vec<DictionaryDetail>,
        ending: DictionaryEnding,
    },
    Set {
        left: String,
        right: String,
        left_only: Vec<String>,
        right_only: Vec<String>,
    },
}

/// Mapping details retain the order of the Python object protocols.
pub enum DictionaryDetail {
    Identical(usize),
    Different(Vec<(String, String)>),
    Extra {
        side: Side,
        count: usize,
        items: Option<Vec<(String, String)>>,
    },
}

/// A failed protocol preserves any details yielded before its failure.
pub enum DictionaryEnding {
    Hint,
    Silent,
    Failure(Option<crate::test_diagnostics::DiagnosticCrash>),
}

pub struct IndexDifference {
    pub index: usize,
    pub left: String,
    pub right: String,
}

pub enum Side {
    Left,
    Right,
}

pub struct ExtraItems {
    pub side: Side,
    pub count: usize,
    pub first: String,
}

fn dictionary(entries: &[(String, String)]) -> String {
    let pairs: Vec<_> = entries
        .iter()
        .map(|(key, value)| format!("{key}: {value}"))
        .collect();
    format!("{{{}}}", pairs.join(", "))
}

fn dictionary_details(
    details: Vec<DictionaryDetail>,
    ending: DictionaryEnding,
    full_diff: Option<Vec<String>>,
) -> Vec<String> {
    let mut lines = Vec::new();
    for detail in details {
        match detail {
            DictionaryDetail::Identical(count) => {
                lines.push(format!("Omitting {count} identical items, use -vv to show"));
            }
            DictionaryDetail::Different(values) => {
                lines.push("Differing items:".to_owned());
                lines.extend(
                    values
                        .into_iter()
                        .map(|(left, right)| format!("{left} != {right}")),
                );
            }
            DictionaryDetail::Extra { side, count, items } => {
                let side = match side {
                    Side::Left => "Left",
                    Side::Right => "Right",
                };
                lines.push(format!(
                    "{side} contains {count} more item{}:",
                    if count == 1 { "" } else { "s" }
                ));
                if let Some(items) = items {
                    lines.push(dictionary(&items));
                }
            }
        }
    }
    match ending {
        DictionaryEnding::Hint => append_iterable_diff(&mut lines, full_diff),
        DictionaryEnding::Silent => (),
        DictionaryEnding::Failure(crash) => lines.extend(representation_failure(crash)),
    }
    lines
}

/// Explain a failed object representation at the location where it was captured.
#[must_use]
pub fn representation_failure(
    crash: Option<crate::test_diagnostics::DiagnosticCrash>,
) -> Vec<String> {
    let crash = crash.map_or_else(
        || "None".to_owned(),
        |crash| {
            format!(
                "{}:{}: {}",
                crash.location.path,
                crash.location.line,
                crash.text.split('\n').next().unwrap_or_default()
            )
        },
    );
    vec![
        format!("(pytest_assertion plugin: representation of details failed: {crash}."),
        " Probably an object has a faulty __repr__.)".to_owned(),
    ]
}

fn append_iterable_diff(lines: &mut Vec<String>, full_diff: Option<Vec<String>>) {
    if let Some(diff) = full_diff {
        if !lines.is_empty() {
            lines.push(String::new());
        }
        lines.push("Full diff:".to_owned());
        lines.extend(diff);
    } else {
        lines.push("Use -v to get more diff".to_owned());
    }
}

impl ContainerComparison {
    /// Render captured object representations without further Python calls.
    #[must_use]
    pub fn render(self) -> String {
        self.render_with_options(
            super::AssertionVerbosity::Normal,
            super::AssertionTruncation::default(),
            None,
        )
    }

    /// Render using the configured assertion verbosity and truncation limits.
    #[must_use]
    pub fn render_with_options(
        self,
        verbosity: super::AssertionVerbosity,
        truncation: super::AssertionTruncation,
        full_diff: Option<Vec<String>>,
    ) -> String {
        let (left, right, mut details) = match self {
            Self::Sequence {
                left,
                right,
                difference,
                extra,
            } => {
                let mut details = Vec::new();
                if let Some(difference) = difference {
                    details.push(format!(
                        "At index {} diff: {} != {}",
                        difference.index, difference.left, difference.right
                    ));
                }
                if let Some(extra) = extra {
                    let side = match extra.side {
                        Side::Left => "Left",
                        Side::Right => "Right",
                    };
                    details.push(if extra.count == 1 {
                        format!("{side} contains one more item: {}", extra.first)
                    } else {
                        format!(
                            "{side} contains {} more items, first extra item: {}",
                            extra.count, extra.first
                        )
                    });
                }
                (left, right, details)
            }
            Self::Dictionary {
                left,
                right,
                details,
                ending,
            } => {
                let lines = dictionary_details(details, ending, full_diff);
                return super::default_difference(
                    format!("{} == {}", shorten(&left, 30), shorten(&right, 30)),
                    lines,
                    verbosity,
                    truncation,
                );
            }
            Self::Set {
                left,
                right,
                left_only,
                right_only,
            } => {
                let mut details = Vec::new();
                if !left_only.is_empty() {
                    details.push("Extra items in the left set:".to_owned());
                    details.extend(left_only);
                }
                if !right_only.is_empty() {
                    details.push("Extra items in the right set:".to_owned());
                    details.extend(right_only);
                }
                (left, right, details)
            }
        };
        append_iterable_diff(&mut details, full_diff);
        super::default_difference(
            format!("{} == {}", shorten(&left, 30), shorten(&right, 30)),
            details,
            verbosity,
            truncation,
        )
    }
}

#[cfg(test)]
#[path = "containers/tests.rs"]
mod tests;
