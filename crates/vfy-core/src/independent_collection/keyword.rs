//! Boolean keyword selection for the independent test frontend.

use unicode_general_category::{GeneralCategory, get_general_category};

const MAX_EXPRESSION_BYTES: usize = 65_536;
const MAX_NESTING: usize = 128;

/// A parsed `-k` filter over case-insensitive keyword substrings.
#[derive(Debug, Eq, PartialEq)]
pub struct KeywordExpression {
    expression: Expression,
}

impl KeywordExpression {
    /// Parse names combined with `and`, `or`, `not`, and parentheses.
    ///
    /// Names accept Unicode letters and numbers, underscores, and the punctuation
    /// `: + - . [ ] / \\`. Operators are lowercase. An empty expression selects
    /// every test, matching an empty command-line keyword filter.
    ///
    /// # Errors
    /// Returns an error with a character column for unsupported characters or
    /// invalid syntax. Expressions larger than 64 KiB or nested beyond 128
    /// negations and parentheses are rejected before matching.
    pub fn parse(source: &str) -> Result<Self, String> {
        parse_expression(source, ExpressionKind::Keyword).map(|expression| Self { expression })
    }

    /// Return whether the expression selects a test node ID.
    #[must_use]
    pub fn matches(&self, nodeid: &str) -> bool {
        let nodeid = nodeid.to_lowercase();
        self.expression
            .matches(&|name| nodeid.contains(&name.to_lowercase()))
    }

    /// Match each keyword separately; substrings cannot cross keyword boundaries.
    #[must_use]
    pub fn matches_keywords(&self, keywords: &[String]) -> bool {
        let names: Vec<_> = keywords.iter().map(|name| name.to_lowercase()).collect();
        self.expression.matches(&|name| {
            let name = name.to_lowercase();
            names.iter().any(|candidate| candidate.contains(&name))
        })
    }
}

/// A parsed `-m` filter over exact, case-sensitive marker names.
#[derive(Debug, Eq, PartialEq)]
pub struct MarkerExpression {
    expression: Expression,
}

impl MarkerExpression {
    /// Parse a boolean marker filter. An empty option leaves every test selected.
    ///
    /// # Errors
    /// Returns an error for invalid syntax, excessive expression size or nesting,
    /// or marker keyword arguments, which are not supported by this matcher.
    pub fn parse(source: &str) -> Result<Self, String> {
        if source.contains('=') {
            return Err("marker keyword arguments are unsupported in -m expressions".to_owned());
        }
        parse_expression(source, ExpressionKind::Marker).map(|expression| Self { expression })
    }

    #[must_use]
    pub fn matches_names(&self, names: &[String]) -> bool {
        self.expression
            .matches(&|name| names.iter().any(|candidate| candidate == name))
    }
}

#[derive(Clone, Copy)]
enum ExpressionKind {
    Keyword,
    Marker,
}

impl ExpressionKind {
    fn flag(self) -> &'static str {
        match self {
            Self::Keyword => "-k",
            Self::Marker => "-m",
        }
    }
}

fn parse_expression(source: &str, kind: ExpressionKind) -> Result<Expression, String> {
    if source.len() > MAX_EXPRESSION_BYTES {
        return Err(format!(
            "invalid {} expression: exceeds 65536 bytes",
            kind.flag()
        ));
    }
    let tokens = tokenize(source, kind)?;
    let mut parser = Parser {
        source,
        tokens,
        position: 0,
        kind,
    };
    if parser.current().kind == TokenKind::End {
        return Ok(Expression::Constant(
            matches!(kind, ExpressionKind::Keyword) || source.is_empty(),
        ));
    }
    let expression = parser.parse_or(0)?;
    if parser.current().kind != TokenKind::End {
        return Err(parser.expected("'and', 'or', or end of input"));
    }
    Ok(expression)
}

#[derive(Debug, Eq, PartialEq)]
enum Expression {
    Constant(bool),
    Name(String),
    And(Vec<Self>),
    Or(Vec<Self>),
    Not(Box<Self>),
}

impl Expression {
    fn matches(&self, matcher: &impl Fn(&str) -> bool) -> bool {
        match self {
            Self::Constant(value) => *value,
            Self::Name(name) => matcher(name),
            Self::And(expressions) => expressions
                .iter()
                .all(|expression| expression.matches(matcher)),
            Self::Or(expressions) => expressions
                .iter()
                .any(|expression| expression.matches(matcher)),
            Self::Not(expression) => !expression.matches(matcher),
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum TokenKind<'a> {
    Name(&'a str),
    And,
    Or,
    Not,
    LeftParen,
    RightParen,
    End,
}

impl TokenKind<'_> {
    fn description(self) -> String {
        match self {
            Self::Name(name) => format!("name {name:?}"),
            Self::And => "'and'".to_owned(),
            Self::Or => "'or'".to_owned(),
            Self::Not => "'not'".to_owned(),
            Self::LeftParen => "'('".to_owned(),
            Self::RightParen => "')'".to_owned(),
            Self::End => "end of input".to_owned(),
        }
    }
}

#[derive(Clone, Copy, Debug)]
struct Token<'a> {
    kind: TokenKind<'a>,
    offset: usize,
}

fn is_name_character(character: char) -> bool {
    matches!(
        get_general_category(character),
        GeneralCategory::UppercaseLetter
            | GeneralCategory::LowercaseLetter
            | GeneralCategory::TitlecaseLetter
            | GeneralCategory::ModifierLetter
            | GeneralCategory::OtherLetter
            | GeneralCategory::DecimalNumber
            | GeneralCategory::LetterNumber
            | GeneralCategory::OtherNumber
    ) || matches!(
        character,
        '_' | ':' | '+' | '-' | '.' | '[' | ']' | '/' | '\\'
    )
}

fn tokenize(source: &str, kind: ExpressionKind) -> Result<Vec<Token<'_>>, String> {
    let mut tokens = Vec::new();
    let mut characters = source.char_indices().peekable();
    while let Some((offset, character)) = characters.next() {
        let kind = match character {
            ' ' | '\t' => continue,
            '(' => TokenKind::LeftParen,
            ')' => TokenKind::RightParen,
            character if is_name_character(character) => {
                while characters
                    .peek()
                    .is_some_and(|(_, next)| is_name_character(*next))
                {
                    characters.next();
                }
                let end = characters
                    .peek()
                    .map_or(source.len(), |(offset, _)| *offset);
                match &source[offset..end] {
                    "and" => TokenKind::And,
                    "or" => TokenKind::Or,
                    "not" => TokenKind::Not,
                    name => TokenKind::Name(name),
                }
            }
            _ => {
                let column = source[..offset].chars().count() + 1;
                return Err(format!(
                    "invalid {} expression at column {column}: unsupported character {character:?}",
                    kind.flag()
                ));
            }
        };
        tokens.push(Token { kind, offset });
    }
    tokens.push(Token {
        kind: TokenKind::End,
        offset: source.len(),
    });
    Ok(tokens)
}

struct Parser<'a> {
    source: &'a str,
    tokens: Vec<Token<'a>>,
    position: usize,
    kind: ExpressionKind,
}

impl<'a> Parser<'a> {
    fn current(&self) -> Token<'a> {
        self.tokens.get(self.position).copied().unwrap_or(Token {
            kind: TokenKind::End,
            offset: self.source.len(),
        })
    }

    fn take(&mut self, kind: TokenKind<'_>) -> bool {
        if self.current().kind == kind {
            self.position += 1;
            true
        } else {
            false
        }
    }

    fn expected(&self, expected: &str) -> String {
        let token = self.current();
        let column = self.source[..token.offset].chars().count() + 1;
        format!(
            "invalid {} expression at column {column}: expected {expected}; got {}",
            self.kind.flag(),
            token.kind.description()
        )
    }

    fn parse_or(&mut self, depth: usize) -> Result<Expression, String> {
        let first = self.parse_and(depth)?;
        let mut expressions = Vec::new();
        while self.take(TokenKind::Or) {
            expressions.push(self.parse_and(depth)?);
        }
        if expressions.is_empty() {
            Ok(first)
        } else {
            expressions.insert(0, first);
            Ok(Expression::Or(expressions))
        }
    }

    fn parse_and(&mut self, depth: usize) -> Result<Expression, String> {
        let first = self.parse_unary(depth)?;
        let mut expressions = Vec::new();
        while self.take(TokenKind::And) {
            expressions.push(self.parse_unary(depth)?);
        }
        if expressions.is_empty() {
            Ok(first)
        } else {
            expressions.insert(0, first);
            Ok(Expression::And(expressions))
        }
    }

    fn parse_unary(&mut self, depth: usize) -> Result<Expression, String> {
        if depth > MAX_NESTING {
            let column = self.source[..self.current().offset].chars().count() + 1;
            return Err(format!(
                "invalid {} expression at column {column}: nesting exceeds {MAX_NESTING}",
                self.kind.flag()
            ));
        }
        if self.take(TokenKind::Not) {
            return Ok(Expression::Not(Box::new(self.parse_unary(depth + 1)?)));
        }
        if self.take(TokenKind::LeftParen) {
            let expression = self.parse_or(depth + 1)?;
            return if self.take(TokenKind::RightParen) {
                Ok(expression)
            } else {
                Err(self.expected("')'"))
            };
        }
        if let TokenKind::Name(name) = self.current().kind {
            self.position += 1;
            return Ok(Expression::Name(name.to_owned()));
        }
        Err(self.expected("a name, 'not', or '('"))
    }
}

#[cfg(test)]
mod tests;
