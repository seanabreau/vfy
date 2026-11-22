//! Path normalization and Coverage.py-compatible glob matching.

use regex::{RegexSet, RegexSetBuilder};
use regex_automata::meta::Regex as AutomataRegex;
use regex_syntax::hir::{Class, ClassUnicode, ClassUnicodeRange, Hir, Look, Repetition};
use std::collections::BTreeSet;
use std::ffi::OsStr;
use std::fmt::Write as _;
use std::path::{Component, Path, PathBuf};

/// Resolve a path and remove platform-specific verbatim prefixes.
///
/// # Errors
///
/// Returns the filesystem error produced when the path cannot be canonicalized.
#[must_use = "use the canonical path or handle the filesystem error"]
pub fn canonical_path(path: &Path) -> std::io::Result<PathBuf> {
    std::fs::canonicalize(path).map(strip_verbatim_prefix)
}

/// Return an absolute, canonicalized path when possible.
#[must_use]
pub fn absolute_path(path: &Path) -> PathBuf {
    let candidate = if path.is_absolute() {
        path.to_path_buf()
    } else {
        std::env::current_dir()
            .unwrap_or_else(|_| PathBuf::from("."))
            .join(path)
    };
    if let Ok(resolved) = canonical_path(&candidate) {
        return resolved;
    }

    // `realpath` resolves symlinks in an existing parent even if the leaf no
    // longer exists. Mirror that behavior for removed and new report paths.
    let mut cursor = candidate.as_path();
    let mut suffix = Vec::new();
    while let Some(parent) = cursor.parent() {
        if let Some(name) = cursor.file_name() {
            suffix.push(name.to_owned());
        }
        if let Ok(mut resolved) = canonical_path(parent) {
            for component in suffix.iter().rev() {
                resolved.push(component);
            }
            return resolved;
        }
        cursor = parent;
    }
    strip_verbatim_prefix(candidate)
}

/// Normalize path separators for glob and report consumers.
#[must_use]
pub fn glob_path(path: &Path) -> String {
    path.to_string_lossy().replace('\\', "/")
}

fn lexical_components(path: &Path) -> Vec<Component<'_>> {
    path.components()
        .filter(|part| !matches!(part, Component::CurDir))
        .collect()
}

/// Compute a relative path from the operands' lexical components.
#[must_use]
pub fn relative_path(root: &Path, source: &Path) -> PathBuf {
    if root.is_absolute() != source.is_absolute() {
        return source.to_path_buf();
    }
    let root_parts = lexical_components(root);
    let source_parts = lexical_components(source);
    let common = root_parts
        .iter()
        .zip(&source_parts)
        .take_while(|(left, right)| left == right)
        .count();
    let rooted = |parts: &[Component<'_>]| {
        parts
            .first()
            .is_some_and(|part| matches!(part, Component::Prefix(_) | Component::RootDir))
    };
    if common == 0 && (rooted(&root_parts) || rooted(&source_parts)) {
        return source.to_path_buf();
    }
    let mut relative = PathBuf::new();
    for _ in common..root_parts.len() {
        relative.push("..");
    }
    for part in &source_parts[common..] {
        relative.push(part.as_os_str());
    }
    relative
}

/// Render the canonical relative path for diagnostics.
#[must_use]
pub fn relative_location(root: &Path, source: &Path) -> String {
    let root = absolute_path(root);
    let source = absolute_path(source);
    glob_path(&relative_path(&root, &source))
}

/// Remove Windows verbatim path prefixes from a path.
#[must_use]
pub fn strip_verbatim_prefix(path: PathBuf) -> PathBuf {
    let text = path.to_string_lossy();
    if let Some(rest) = text.strip_prefix(r"\\?\UNC\") {
        return PathBuf::from(format!(r"\\{rest}"));
    }
    if let Some(rest) = text.strip_prefix(r"\\?\") {
        return PathBuf::from(rest);
    }
    path
}

#[cfg(test)]
mod lexical_tests;
#[cfg(test)]
mod tests;

/// List the `.py` and `.pyw` files under `root` as sorted absolute paths.
///
/// Subdirectories are searched only when they contain `__init__.py`. Unreadable
/// directories are skipped.
#[must_use]
pub fn discover_python_source_files(root: &Path) -> Vec<PathBuf> {
    let mut files = BTreeSet::new();
    discover_python_source_files_in_directory(root, true, &mut files);
    files.into_iter().collect()
}

fn discover_python_source_files_in_directory(
    directory: &Path,
    source_root: bool,
    files: &mut BTreeSet<PathBuf>,
) {
    let Ok(entries) = std::fs::read_dir(directory) else {
        return;
    };
    let entries: Vec<_> = entries.flatten().collect();
    if !source_root
        && !entries
            .iter()
            .any(|entry| entry.file_name() == OsStr::new("__init__.py") && entry.path().is_file())
    {
        return;
    }

    for entry in entries {
        let path = entry.path();
        let Ok(file_type) = entry.file_type() else {
            continue;
        };
        if file_type.is_dir() {
            discover_python_source_files_in_directory(&path, false, files);
        } else if !(file_type.is_symlink() && path.is_dir())
            && (file_type.is_file() || file_type.is_symlink())
            && is_python_source_file(&path)
        {
            files.insert(absolute_path(&path));
        }
    }
}

fn is_python_source_file(path: &Path) -> bool {
    let Some(name) = path.file_name().and_then(OsStr::to_str) else {
        return false;
    };
    let Some((stem, extension)) = name.rsplit_once('.') else {
        return false;
    };
    !stem.is_empty()
        && matches!(extension, "py" | "pyw")
        && !stem
            .chars()
            .any(|character| ".#~!$@%^&*()+=,".contains(character))
}

/// An invalid Coverage.py file pattern or a glob compilation failure.
#[derive(Debug)]
pub enum PathPatternError {
    InvalidToken(String),
    Compile(regex::Error),
    VersionedCompile(Box<regex_automata::meta::BuildError>),
}

impl std::fmt::Display for PathPatternError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::InvalidToken(token) => {
                write!(
                    formatter,
                    "File pattern can't include {}",
                    quote_python_string(token)
                )
            }
            Self::Compile(error) => error.fmt(formatter),
            Self::VersionedCompile(error) => error.fmt(formatter),
        }
    }
}

pub(crate) fn quote_python_string(token: &str) -> String {
    let quote = if token.contains('\'') && !token.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut quoted = String::from(quote);
    for character in token.chars() {
        match character {
            '\\' => quoted.push_str("\\\\"),
            '\n' => quoted.push_str("\\n"),
            '\r' => quoted.push_str("\\r"),
            '\t' => quoted.push_str("\\t"),
            control if control == quote => {
                quoted.push('\\');
                quoted.push(control);
            }
            control if control.is_control() && u32::from(control) <= 0xff => {
                let _ = write!(quoted, "\\x{:02x}", u32::from(control));
            }
            control if control.is_control() => {
                let _ = write!(quoted, "\\u{:04x}", u32::from(control));
            }
            printable => quoted.push(printable),
        }
    }
    quoted.push(quote);
    quoted
}

impl std::error::Error for PathPatternError {}

impl From<regex::Error> for PathPatternError {
    fn from(error: regex::Error) -> Self {
        Self::Compile(error)
    }
}

/// Compiled Coverage.py file patterns shared by collection and reporting.
#[derive(Clone, Debug)]
pub enum CoverageGlobSet {
    Regular(RegexSet),
    Versioned(AutomataRegex),
}

impl CoverageGlobSet {
    /// Return whether any file pattern matches the complete path.
    #[must_use]
    pub fn is_match(&self, path: impl AsRef<str>) -> bool {
        match self {
            Self::Regular(patterns) => patterns.is_match(path.as_ref()),
            Self::Versioned(patterns) => patterns.is_match(path.as_ref()),
        }
    }
}

/// Unicode case-folding tables used by the active Python interpreter.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum UnicodeCaseVersion {
    /// Python 3.12 and 3.13 use Unicode 15 and 15.1, with the same case pairs.
    V15,
    /// Python 3.14 and the bundled Rust regex tables use Unicode 16.
    #[default]
    V16,
    /// Python 3.15 uses Unicode 17.
    V17,
}

impl UnicodeCaseVersion {
    /// Select the Unicode case table for an active `CPython` minor version.
    #[must_use]
    pub const fn for_python_minor(minor: u8) -> Self {
        if minor <= 13 {
            Self::V15
        } else if minor == 14 {
            Self::V16
        } else {
            Self::V17
        }
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
pub(crate) enum CaseSensitivity {
    Sensitive,
    Insensitive,
}

impl CaseSensitivity {
    const fn platform() -> Self {
        if cfg!(windows) {
            Self::Insensitive
        } else {
            Self::Sensitive
        }
    }
}

// Coverage.py's tokenizer rejects these tokens before recognizing wildcards or
// character classes. Keep the same priority and consumed fragment for diagnostics.
fn invalid_glob_token(pattern: &str) -> Option<&str> {
    let component = pattern.split('/').next()?;
    let leading_stars = component.bytes().take_while(|byte| *byte == b'*').count();
    if leading_stars >= 3 {
        return Some(&component[..leading_stars]);
    }
    if let Some(offset) = component.rfind("**").filter(|offset| *offset > 0) {
        return Some(&component[..offset + 2]);
    }
    if leading_stars == 2 && component.len() > 2 {
        return Some(component);
    }
    if pattern.starts_with("**/**") {
        return Some(&pattern[..5]);
    }
    None
}

fn translate_glob_class(class: &str, translated: &mut String, case: CaseSensitivity) {
    // Encoding literal members avoids regex set-operator syntax (`&&`, `--`,
    // `||`, `~~`) that Python treats as ordinary class members.
    let members: Vec<_> = class[1..class.len() - 1].chars().collect();
    translated.push('[');
    let mut index = 0;
    if members.first() == Some(&'^') {
        translated.push('^');
        index = 1;
    }
    let mut includes_i = false;
    while index < members.len() {
        if index + 2 < members.len() && members[index + 1] == '-' {
            includes_i |= ['i', 'I', 'İ', 'ı']
                .into_iter()
                .any(|character| members[index] <= character && character <= members[index + 2]);
            push_glob_class_member(members[index], translated);
            translated.push('-');
            push_glob_class_member(members[index + 2], translated);
            index += 3;
        } else {
            includes_i |= matches!(members[index], 'i' | 'I' | 'İ' | 'ı');
            push_glob_class_member(members[index], translated);
            index += 1;
        }
    }
    // Python's IGNORECASE also folds dotted and dotless I; Rust regex does not.
    if case == CaseSensitivity::Insensitive && includes_i {
        for character in ['i', 'I', 'İ', 'ı'] {
            push_glob_class_member(character, translated);
        }
    }
    translated.push(']');
}

fn push_glob_class_member(character: char, translated: &mut String) {
    let _ = write!(translated, "\\x{{{:x}}}", u32::from(character));
}

enum GlobToken {
    OptionalDirectory,
    TrailingAny,
    CharacterClass(String),
    Star,
    Question,
    Literal(char),
}

fn tokenize_glob(pattern: &str) -> Result<Vec<GlobToken>, PathPatternError> {
    let normalized = pattern.replace('\\', "/");
    let normalized = if normalized.contains('/') {
        normalized
    } else {
        format!("**/{normalized}")
    };
    let mut tokens = Vec::new();
    let mut offset = 0;
    while offset < normalized.len() {
        let remaining = &normalized[offset..];
        if let Some(token) = invalid_glob_token(remaining) {
            return Err(PathPatternError::InvalidToken(token.to_owned()));
        }
        let stars = remaining.bytes().take_while(|byte| *byte == b'*').count();
        if offset == 0 && stars > 0 && remaining[stars..].starts_with('/') {
            tokens.push(GlobToken::OptionalDirectory);
            offset += stars + 1;
        } else if remaining.starts_with('/')
            && remaining.len() > 1
            && remaining[1..].bytes().all(|byte| byte == b'*')
        {
            tokens.push(GlobToken::TrailingAny);
            break;
        } else if remaining.starts_with("**/") {
            tokens.push(GlobToken::OptionalDirectory);
            offset += 3;
        } else if remaining.starts_with('[') {
            let Some(end) = remaining.find(']') else {
                return Err(PathPatternError::InvalidToken("[".to_owned()));
            };
            if end == 1 {
                return Err(PathPatternError::InvalidToken("[".to_owned()));
            }
            tokens.push(GlobToken::CharacterClass(remaining[..=end].to_owned()));
            offset += end + 1;
        } else {
            let Some(character) = remaining.chars().next() else {
                break;
            };
            if character == ']' {
                return Err(PathPatternError::InvalidToken("]".to_owned()));
            }
            tokens.push(match character {
                '*' => GlobToken::Star,
                '?' => GlobToken::Question,
                _ => GlobToken::Literal(character),
            });
            offset += character.len_utf8();
        }
    }
    Ok(tokens)
}

fn coverage_glob(tokens: &[GlobToken], case: CaseSensitivity) -> String {
    let mut translated = String::from("^(?:");
    for token in tokens {
        match token {
            GlobToken::OptionalDirectory => translated.push_str("(?:.*/)?"),
            GlobToken::TrailingAny => translated.push_str("/.*"),
            GlobToken::CharacterClass(class) => translate_glob_class(class, &mut translated, case),
            GlobToken::Star => translated.push_str("[^/]*"),
            GlobToken::Question => translated.push_str("[^/]"),
            GlobToken::Literal('i' | 'I' | 'İ' | 'ı') if case == CaseSensitivity::Insensitive => {
                translated.push_str("[iIİı]");
            }
            GlobToken::Literal(character) => {
                translated.push_str(&regex::escape(&character.to_string()));
            }
        }
    }
    translated.push_str(")\\z");
    translated
}

fn class_contains(class: &ClassUnicode, character: char) -> bool {
    class
        .iter()
        .any(|range| range.start() <= character && character <= range.end())
}

// Simple case pairs that CPython's Unicode 16 and 17 tables add. Emit them as
// explicit literals even when an older interpreter treats them as uncased;
// `re.IGNORECASE` results were checked in both directions.
fn unicode_case_pairs(version: UnicodeCaseVersion) -> Vec<(char, char)> {
    let (mut pairs, (first, second, count)) = match version {
        UnicodeCaseVersion::V15 => (
            vec![
                ('\u{019b}', '\u{a7dc}'),
                ('\u{0264}', '\u{a7cb}'),
                ('\u{1c89}', '\u{1c8a}'),
                ('\u{a7cc}', '\u{a7cd}'),
                ('\u{a7da}', '\u{a7db}'),
            ],
            (0x10d50, 0x10d70, 22),
        ),
        UnicodeCaseVersion::V16 => return Vec::new(),
        UnicodeCaseVersion::V17 => (
            vec![
                ('\u{a7ce}', '\u{a7cf}'),
                ('\u{a7d2}', '\u{a7d3}'),
                ('\u{a7d4}', '\u{a7d5}'),
            ],
            (0x16ea0, 0x16ebb, 25),
        ),
    };
    pairs.extend((0..count).map(|offset| {
        (
            char::from_u32(first + offset).expect("case-pair ranges contain only Unicode scalars"),
            char::from_u32(second + offset).expect("case-pair ranges contain only Unicode scalars"),
        )
    }));
    pairs
}

fn folded_class(base: &ClassUnicode, version: UnicodeCaseVersion) -> ClassUnicode {
    let mut members = base.clone();
    if ['i', 'I', 'İ', 'ı']
        .into_iter()
        .any(|character| class_contains(base, character))
    {
        for character in ['i', 'I', 'İ', 'ı'] {
            members.push(ClassUnicodeRange::new(character, character));
        }
    }
    let mut folded = members.clone();
    folded.case_fold_simple();
    let pairs = unicode_case_pairs(version);
    if version == UnicodeCaseVersion::V15 {
        let delta = ClassUnicode::new(pairs.iter().flat_map(|&(first, second)| {
            [
                ClassUnicodeRange::new(first, first),
                ClassUnicodeRange::new(second, second),
            ]
        }));
        folded.difference(&delta);
        let mut original = members;
        original.intersect(&delta);
        folded.union(&original);
    } else if version == UnicodeCaseVersion::V17 {
        for (first, second) in pairs {
            if class_contains(&members, first) || class_contains(&members, second) {
                folded.push(ClassUnicodeRange::new(first, first));
                folded.push(ClassUnicodeRange::new(second, second));
            }
        }
    }
    folded
}

fn unicode_class(members: &ClassUnicode, negated: bool, version: UnicodeCaseVersion) -> Hir {
    let mut members = folded_class(members, version);
    if negated {
        members.negate();
    }
    Hir::class(Class::Unicode(members))
}

fn literal_hir(character: char, version: UnicodeCaseVersion) -> Hir {
    unicode_class(
        &ClassUnicode::new([ClassUnicodeRange::new(character, character)]),
        false,
        version,
    )
}

fn class_hir(class: &str, version: UnicodeCaseVersion) -> Hir {
    let members: Vec<_> = class[1..class.len() - 1].chars().collect();
    let negated = members.first() == Some(&'^');
    let mut index = usize::from(negated);
    let mut ranges = Vec::new();
    while index < members.len() {
        if index + 2 < members.len() && members[index + 1] == '-' {
            ranges.push(ClassUnicodeRange::new(members[index], members[index + 2]));
            index += 3;
        } else {
            ranges.push(ClassUnicodeRange::new(members[index], members[index]));
            index += 1;
        }
    }
    unicode_class(&ClassUnicode::new(ranges), negated, version)
}

fn repeat_hir(sub: Hir, min: u32, max: Option<u32>) -> Hir {
    Hir::repetition(Repetition {
        min,
        max,
        greedy: true,
        sub: Box::new(sub),
    })
}

fn any_except_hir(excluded: char) -> Hir {
    let mut members = ClassUnicode::new([ClassUnicodeRange::new('\0', '\u{10ffff}')]);
    members.difference(&ClassUnicode::new([ClassUnicodeRange::new(
        excluded, excluded,
    )]));
    Hir::class(Class::Unicode(members))
}

fn optional_directory_hir(version: UnicodeCaseVersion) -> Hir {
    repeat_hir(
        Hir::concat(vec![
            repeat_hir(any_except_hir('\n'), 0, None),
            literal_hir('/', version),
        ]),
        0,
        Some(1),
    )
}

// Compile the shared glob tokens with explicit case-folded classes so Python's
// Unicode version can be applied before class negation.
fn coverage_glob_hir(tokens: &[GlobToken], version: UnicodeCaseVersion) -> Hir {
    let mut pieces = vec![Hir::look(Look::Start)];
    for token in tokens {
        pieces.push(match token {
            GlobToken::OptionalDirectory => optional_directory_hir(version),
            GlobToken::TrailingAny => Hir::concat(vec![
                literal_hir('/', version),
                repeat_hir(any_except_hir('\n'), 0, None),
            ]),
            GlobToken::CharacterClass(class) => class_hir(class, version),
            GlobToken::Star => repeat_hir(any_except_hir('/'), 0, None),
            GlobToken::Question => any_except_hir('/'),
            GlobToken::Literal(character) => literal_hir(*character, version),
        });
    }
    pieces.push(Hir::look(Look::End));
    Hir::concat(pieces)
}

/// Compile Coverage.py file patterns with literal braces and validated wildcards.
///
/// # Errors
/// Returns the rejected Coverage.py token or a glob compilation error.
pub fn glob_set(patterns: &[String]) -> Result<Option<CoverageGlobSet>, PathPatternError> {
    glob_set_with_unicode_version(patterns, UnicodeCaseVersion::default())
}

/// Compile file patterns against the active Python interpreter's Unicode tables.
///
/// # Errors
/// Returns the rejected Coverage.py token or a regex compilation error.
pub fn glob_set_with_unicode_version(
    patterns: &[String],
    version: UnicodeCaseVersion,
) -> Result<Option<CoverageGlobSet>, PathPatternError> {
    glob_set_with_case_and_version(patterns, CaseSensitivity::platform(), version)
}

pub(crate) fn glob_set_with_case(
    patterns: &[String],
    case: CaseSensitivity,
) -> Result<Option<CoverageGlobSet>, PathPatternError> {
    glob_set_with_case_and_version(patterns, case, UnicodeCaseVersion::default())
}

fn glob_set_with_case_and_version(
    patterns: &[String],
    case: CaseSensitivity,
    version: UnicodeCaseVersion,
) -> Result<Option<CoverageGlobSet>, PathPatternError> {
    if patterns.is_empty() {
        return Ok(None);
    }
    let tokens = patterns
        .iter()
        .map(|pattern| tokenize_glob(pattern))
        .collect::<Result<Vec<_>, _>>()?;
    let translated = tokens
        .iter()
        .map(|tokens| coverage_glob(tokens, case))
        .collect::<Vec<_>>();
    let regular = RegexSetBuilder::new(translated)
        .case_insensitive(case == CaseSensitivity::Insensitive)
        .build()
        .map_err(PathPatternError::from)?;
    if case == CaseSensitivity::Insensitive && version != UnicodeCaseVersion::V16 {
        let hirs = tokens
            .iter()
            .map(|tokens| coverage_glob_hir(tokens, version))
            .collect::<Vec<_>>();
        return AutomataRegex::builder()
            .build_many_from_hir(&hirs)
            .map(CoverageGlobSet::Versioned)
            .map(Some)
            .map_err(|error| PathPatternError::VersionedCompile(Box::new(error)));
    }
    Ok(Some(CoverageGlobSet::Regular(regular)))
}
