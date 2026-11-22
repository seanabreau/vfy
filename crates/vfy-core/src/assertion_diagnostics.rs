//! Assertion expression descriptions captured by the `CPython` compiler adapter.

use serde::Deserialize;
use std::fmt::Write;

#[path = "assertion_diagnostics/containers.rs"]
mod containers;
#[path = "assertion_diagnostics/strings.rs"]
mod strings;
pub use containers::{
    ContainerComparison, DictionaryDetail, DictionaryEnding, ExtraItems, IndexDifference, Side,
    representation_failure,
};

/// Bound an already captured object representation without Python callbacks.
#[must_use]
pub fn shorten_representation(value: &str, limit: usize) -> String {
    strings::shorten(value, limit)
}

/// Prepared text and notices shared by native assertion and exception diagnostics.
///
/// The caller supplies its text-difference primitive and, when requested, exact
/// language-specific representations of the prepared strings.
pub struct TextDifferenceInput {
    /// Left text after verbosity-dependent context trimming.
    pub left: String,
    /// Right text after verbosity-dependent context trimming.
    pub right: String,
    /// Context notices emitted before the difference itself.
    pub notices: Vec<String>,
    /// Whether either prepared string consists entirely of Python whitespace.
    pub escape_whitespace: bool,
}

/// Prepare the shared context and whitespace policy without Python callbacks.
#[must_use]
pub fn prepare_text_difference(left: &str, right: &str, verbosity: u32) -> TextDifferenceInput {
    strings::prepare_difference(left, right, verbosity)
}

/// Finite rendering policy for a configured assertion verbosity level.
#[derive(Clone, Copy, Default)]
pub enum AssertionVerbosity {
    #[default]
    Normal,
    Verbose,
    Full,
}

/// Limits applied to the explanation for a failed comparison.
#[derive(Clone, Copy)]
pub struct AssertionTruncation {
    /// Maximum lines, or zero to disable the line limit.
    pub max_lines: i64,
    /// Maximum characters, or zero to disable the character limit.
    pub max_chars: i64,
    /// Whether the test runs under a continuous integration environment.
    pub on_ci: bool,
}

impl Default for AssertionTruncation {
    fn default() -> Self {
        Self {
            max_lines: 8,
            max_chars: 640,
            on_ci: false,
        }
    }
}

impl AssertionVerbosity {
    fn representation(self, text: &str) -> String {
        match self {
            Self::Normal => strings::shorten(text, 240),
            Self::Verbose => strings::shorten(text, 2400),
            Self::Full => text.to_owned(),
        }
    }

    fn summary(self, text: &str) -> String {
        match self {
            Self::Full => text.to_owned(),
            Self::Normal | Self::Verbose => strings::shorten(text, 30),
        }
    }
}

fn default_difference(
    summary: String,
    details: Vec<String>,
    verbosity: AssertionVerbosity,
    truncation: AssertionTruncation,
) -> String {
    let mut lines = vec![summary, String::new()];
    lines.extend(details);
    truncated_lines_with_limits(lines, verbosity, truncation).join("\n  ")
}

/// Render the explanation a `pytest_assertrepr_compare` implementation returned.
///
/// Pytest truncates the plug-in's lines like its own, then escapes embedded newlines so each
/// element stays on one line.
#[must_use]
pub fn render_hook_explanation(
    lines: Vec<String>,
    verbosity: AssertionVerbosity,
    truncation: AssertionTruncation,
) -> String {
    let escaped = truncated_lines_with_limits(lines, verbosity, truncation)
        .into_iter()
        .map(|line| line.replace('\n', "\\n"))
        .collect::<Vec<_>>();
    escaped.join("\n  ")
}

fn truncated_lines_with_limits(
    mut lines: Vec<String>,
    verbosity: AssertionVerbosity,
    limits: AssertionTruncation,
) -> Vec<String> {
    let characters: usize = lines.iter().map(|line| line.chars().count()).sum();
    let within_lines =
        limits.max_lines == 0 || (lines.len() as i128) <= i128::from(limits.max_lines) + 2;
    let within_chars =
        limits.max_chars == 0 || (characters as i128) <= i128::from(limits.max_chars) + 70;
    if !limits.on_ci
        && !matches!(verbosity, AssertionVerbosity::Full)
        && (limits.max_lines > 0 || limits.max_chars > 0)
        && !(within_lines && within_chars)
    {
        let original_lines = lines.len();
        if !within_lines {
            let count = if limits.max_lines >= 0 {
                usize::try_from(limits.max_lines).unwrap_or(usize::MAX)
            } else {
                lines.len().saturating_sub(
                    usize::try_from(limits.max_lines.unsigned_abs()).unwrap_or(usize::MAX),
                )
            };
            lines.truncate(count);
        }
        let cut_characters = limits.max_chars > 0
            && (lines.iter().map(|line| line.chars().count()).sum::<usize>() as i128)
                > i128::from(limits.max_chars) + 70;
        if cut_characters {
            let mut remaining = usize::try_from(limits.max_chars).unwrap_or(usize::MAX);
            let mut keep = 0;
            for line in &mut lines {
                let count = line.chars().count();
                keep += 1;
                if count > remaining {
                    *line = line.chars().take(remaining).collect();
                    break;
                }
                remaining -= count;
            }
            lines.truncate(keep);
        }
        if let Some(last) = lines.last_mut() {
            last.push_str("...");
        }
        let hidden = original_lines.saturating_sub(lines.len()) + usize::from(cut_characters);
        lines.push(String::new());
        lines.push(format!(
            "...Full output truncated ({hidden} line{} hidden), use '-vv' to show",
            if hidden == 1 { "" } else { "s" }
        ));
    }
    lines
}

/// Render a comparison whose detail lines came from the compared object's own explanation.
#[must_use]
pub fn render_protocol_comparison_with_truncation(
    left: &str,
    right: &str,
    details: Vec<String>,
    verbosity: AssertionVerbosity,
    truncation: AssertionTruncation,
) -> String {
    default_difference(
        format!(
            "{} == {}",
            verbosity.summary(left),
            verbosity.summary(right)
        ),
        details,
        verbosity,
        truncation,
    )
}

#[derive(Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
enum Expression {
    Value {
        value: String,
    },
    Binary {
        operator: String,
        left: Box<Self>,
        right: Box<Self>,
    },
    Unary {
        operator: String,
        operand: Box<Self>,
    },
    Attribute {
        value: String,
        owner: Box<Self>,
        name: String,
    },
    Call {
        value: String,
        function: Box<Self>,
        arguments: Vec<Argument>,
        keywords: Vec<Keyword>,
    },
    Comparison {
        selected: usize,
        operators: Vec<String>,
        operands: Vec<Self>,
        #[serde(default)]
        strings: Option<[String; 4]>,
        #[serde(default)]
        builtin: Option<String>,
    },
    Boolean {
        operator: BooleanOperator,
        operands: Vec<Self>,
    },
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Argument {
    unpack: bool,
    expression: Expression,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Keyword {
    name: Option<String>,
    expression: Expression,
}

#[derive(Clone, Copy, Deserialize)]
#[serde(rename_all = "snake_case")]
enum BooleanOperator {
    And,
    Or,
}

struct RenderedExpression {
    text: String,
    details: Vec<Detail>,
}

struct Detail {
    value: String,
    source: String,
    children: Vec<Self>,
}

impl Expression {
    fn render(
        self,
        verbosity: AssertionVerbosity,
        truncation: AssertionTruncation,
    ) -> Result<RenderedExpression, String> {
        match self {
            Self::Value { value } => Ok(RenderedExpression {
                text: verbosity.representation(&value),
                details: Vec::new(),
            }),
            Self::Binary {
                operator,
                left,
                right,
            } => {
                let left = left.render(verbosity, truncation)?;
                let right = right.render(verbosity, truncation)?;
                let mut details = left.details;
                details.extend(right.details);
                Ok(RenderedExpression {
                    text: format!("({} {operator} {})", left.text, right.text),
                    details,
                })
            }
            Self::Unary { operator, operand } => {
                let operand = operand.render(verbosity, truncation)?;
                Ok(RenderedExpression {
                    text: operator + &operand.text,
                    details: operand.details,
                })
            }
            Self::Attribute { value, owner, name } => {
                let value = verbosity.representation(&value);
                let owner = owner.render(verbosity, truncation)?;
                let detail = Detail {
                    value: value.clone(),
                    source: format!("{}.{name}", owner.text),
                    children: owner.details,
                };
                Ok(RenderedExpression {
                    text: value,
                    details: vec![detail],
                })
            }
            Self::Call {
                value,
                function,
                arguments,
                keywords,
            } => render_call(
                &value, *function, arguments, keywords, verbosity, truncation,
            ),
            Self::Comparison {
                selected,
                operators,
                operands,
                strings,
                builtin,
            } => {
                if operators.len().checked_add(1) != Some(operands.len())
                    || selected >= operators.len()
                {
                    return Err(
                        "assertion comparison has inconsistent operands and operators".to_owned(),
                    );
                }
                if let Some(text) = builtin {
                    return Ok(RenderedExpression {
                        text,
                        details: Vec::new(),
                    });
                }
                let mut rendered = operands
                    .into_iter()
                    .map(|expression| expression.render(verbosity, truncation))
                    .collect::<Result<Vec<_>, _>>()?;
                let right = rendered.remove(selected + 1);
                let left = rendered.remove(selected);
                if let Some([actual, expected, actual_repr, expected_repr]) =
                    strings.filter(|_| operators[selected] == "==")
                {
                    // Pytest summarizes the operand values, not their source expressions.
                    return Ok(RenderedExpression {
                        text: strings::equality(
                            &actual,
                            &expected,
                            &verbosity.representation(&actual_repr),
                            &verbosity.representation(&expected_repr),
                            verbosity,
                            truncation,
                        ),
                        details: Vec::new(),
                    });
                }
                let mut details = left.details;
                details.extend(right.details);
                Ok(RenderedExpression {
                    text: format!("{} {} {}", left.text, operators[selected], right.text),
                    details,
                })
            }
            Self::Boolean { operator, operands } => {
                render_boolean(operator, operands, verbosity, truncation)
            }
        }
    }
}

fn render_boolean(
    operator: BooleanOperator,
    operands: Vec<Expression>,
    verbosity: AssertionVerbosity,
    truncation: AssertionTruncation,
) -> Result<RenderedExpression, String> {
    let rendered = operands
        .into_iter()
        .map(|expression| expression.render(verbosity, truncation))
        .collect::<Result<Vec<_>, _>>()?;
    let operator = match operator {
        BooleanOperator::And => " and ",
        BooleanOperator::Or => " or ",
    };
    let mut pieces = Vec::new();
    let mut details = Vec::new();
    for operand in rendered {
        pieces.push(operand.text);
        details.extend(operand.details);
    }
    Ok(RenderedExpression {
        text: format!("({})", pieces.join(operator)),
        details,
    })
}

fn render_call(
    value: &str,
    function: Expression,
    arguments: Vec<Argument>,
    keywords: Vec<Keyword>,
    verbosity: AssertionVerbosity,
    truncation: AssertionTruncation,
) -> Result<RenderedExpression, String> {
    let value = verbosity.representation(value);
    let function = function.render(verbosity, truncation)?;
    let mut children = function.details;
    let mut pieces = Vec::new();
    for argument in arguments {
        let rendered = argument.expression.render(verbosity, truncation)?;
        pieces.push(if argument.unpack {
            format!("*{}", rendered.text)
        } else {
            rendered.text
        });
        children.extend(rendered.details);
    }
    for keyword in keywords {
        let rendered = keyword.expression.render(verbosity, truncation)?;
        pieces.push(match keyword.name {
            Some(name) => format!("{name}={}", rendered.text),
            None => format!("**{}", rendered.text),
        });
        children.extend(rendered.details);
    }
    let detail = Detail {
        value: value.clone(),
        source: format!("{}({})", function.text, pieces.join(", ")),
        children,
    };
    Ok(RenderedExpression {
        text: value,
        details: vec![detail],
    })
}

fn append_details(output: &mut String, details: &[Detail], depth: usize) {
    for (index, detail) in details.iter().enumerate() {
        let connector = if index == 0 { "where " } else { "and   " };
        let _ = write!(
            output,
            "\n +{}{}{} = {}",
            " ".repeat(depth * 2 + 2),
            connector,
            detail.value,
            detail.source
        );
        append_details(output, &detail.children, depth + 1);
    }
}

/// Render captured representations without calling Python objects again.
///
/// # Errors
///
/// Rejects malformed expression records and inconsistent comparison shapes.
pub fn render_assertion_explanation(
    message: Option<&str>,
    expression_json: &str,
) -> Result<String, String> {
    render_assertion_explanation_with_options(
        message,
        expression_json,
        AssertionVerbosity::Normal,
        AssertionTruncation::default(),
    )
}

/// Render captured expressions with the configured assertion limits.
///
/// # Errors
///
/// Rejects malformed expression records and inconsistent comparison shapes.
pub fn render_assertion_explanation_with_options(
    message: Option<&str>,
    expression_json: &str,
    verbosity: AssertionVerbosity,
    truncation: AssertionTruncation,
) -> Result<String, String> {
    // The adapter's CPython AST can exceed serde's default JSON nesting limit.
    // This wire is generated in-process after the source has compiled.
    let mut decoder = serde_json::Deserializer::from_str(expression_json);
    decoder.disable_recursion_limit();
    let expression = Expression::deserialize(&mut decoder)
        .map_err(|error| format!("invalid assertion expression: {error}"))?;
    decoder
        .end()
        .map_err(|error| format!("invalid assertion expression: {error}"))?;
    let rendered = expression.render(verbosity, truncation)?;
    let prefix = message.map_or_else(String::new, |message| {
        format!("{}\n", message.replace('\n', "\n  "))
    });
    let mut output = format!("{prefix}assert {}", rendered.text);
    append_details(&mut output, &rendered.details, 0);
    Ok(output)
}

#[cfg(test)]
mod tests;
