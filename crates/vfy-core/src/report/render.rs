//! Report rendering and atomic filesystem commits.

use std::borrow::Cow;
use std::collections::{BTreeMap, BTreeSet};
use std::fmt::Write;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use serde::ser::{Serialize, SerializeMap, SerializeSeq, Serializer};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use sha3::Sha3_256;

use crate::html::escape as html_escape;

use super::{
    Arc, CodeRegion, ReportCounts, ReportDisplay, ReportError, ReportResult, ReportRow,
    partial_branch_count, total_counts,
};

#[cfg(test)]
mod tests;

mod unicode;
pub use unicode::UnicodeVersion;

fn ranges(numbers: &BTreeSet<i32>) -> String {
    let mut result = Vec::new();
    let mut values = numbers.iter().copied();
    let Some(mut start) = values.next() else {
        return String::new();
    };
    let mut previous = start;
    for value in values {
        if value != previous + 1 {
            result.push(if start == previous {
                start.to_string()
            } else {
                format!("{start}-{previous}")
            });
            start = value;
        }
        previous = value;
    }
    result.push(if start == previous {
        start.to_string()
    } else {
        format!("{start}-{previous}")
    });
    result.join(", ")
}

/// Format sorted line numbers as comma-separated runs such as `1-3, 7`.
#[must_use]
pub fn format_line_ranges(lines: Vec<i32>) -> String {
    ranges(&lines.into_iter().collect())
}

/// Count destinations and taken destinations for each line with more than one possible arc.
///
/// Returns `line -> (possible, taken)`.
#[must_use]
pub fn branch_statistics(possible: Vec<Arc>, observed: Vec<Arc>) -> BTreeMap<i32, (usize, usize)> {
    let observed: BTreeSet<Arc> = observed.into_iter().collect();
    let mut destinations: BTreeMap<i32, BTreeSet<i32>> = BTreeMap::new();
    let mut taken: BTreeMap<i32, BTreeSet<i32>> = BTreeMap::new();
    for (start, end) in possible {
        destinations.entry(start).or_default().insert(end);
        if observed.contains(&(start, end)) {
            taken.entry(start).or_default().insert(end);
        }
    }
    destinations
        .into_iter()
        .filter(|(_, ends)| ends.len() > 1)
        .map(|(line, ends)| {
            (
                line,
                (ends.len(), taken.get(&line).map_or(0, BTreeSet::len)),
            )
        })
        .collect()
}

/// Format a percentage so that only no coverage or full coverage displays an endpoint.
#[must_use]
pub fn display_percent(percent: f64, precision: usize) -> String {
    let increment = 10.0_f64.powi(-i32::try_from(precision).unwrap_or(i32::MAX));
    // A displayed endpoint must mean no coverage or complete coverage.
    let displayed = if 0.0 < percent && percent < increment {
        increment
    } else if 100.0 - increment < percent && percent < 100.0 {
        100.0 - increment
    } else {
        percent
    };
    format!("{displayed:.precision$}")
}

/// Color a terminal coverage cell by how much of the file is covered.
fn colored_cell(cell: &str, percent: f64, color: bool) -> String {
    if !color {
        return cell.to_owned();
    }
    let code = if percent >= 100.0 {
        "32"
    } else if percent >= 50.0 {
        "33"
    } else {
        "31"
    };
    format!("\u{1b}[{code}m{cell}\u{1b}[0m")
}

/// Return Coverage.py's `relative_filename()`: the path below the reporting
/// directory, or the stored path for files elsewhere.
pub(super) fn terminal_filename(row: &ReportRow) -> Cow<'_, str> {
    if let Some(display) = row
        .report_file_paths
        .as_ref()
        .and_then(|paths| paths.display.as_deref())
    {
        return Cow::Borrowed(display);
    }
    let Ok(directory) = std::env::current_dir() else {
        return Cow::Borrowed(&row.path);
    };
    let mut prefix = directory.to_string_lossy().into_owned();
    if !prefix.ends_with(std::path::MAIN_SEPARATOR) {
        prefix.push(std::path::MAIN_SEPARATOR);
    }
    let matches = if cfg!(windows) {
        row.path
            .get(..prefix.len())
            .is_some_and(|start| start.eq_ignore_ascii_case(&prefix))
    } else {
        row.path.starts_with(&prefix)
    };
    if matches {
        Cow::Borrowed(&row.path[prefix.len()..])
    } else {
        Cow::Borrowed(&row.path)
    }
}

/// Format missing lines as Coverage.py's `Analysis.missing_formatted(branches=True)`.
///
/// Ranges join consecutive missing statements, even across non-statement lines.
/// A missing branch appears as `line->destination` when neither end is a missing line.
fn missing_formatted(row: &ReportRow) -> String {
    let missing = row.missing_set();
    let mut items: Vec<(i32, String)> = Vec::new();
    let mut range: Option<(i32, i32)> = None;
    for &statement in &row.executable {
        if missing.contains(&statement) {
            range = Some(range.map_or((statement, statement), |(start, _)| (start, statement)));
        } else if let Some((start, end)) = range.take() {
            items.push((start, nice_pair(start, end)));
        }
    }
    if let Some((start, end)) = range {
        items.push((start, nice_pair(start, end)));
    }
    for (origin, destination) in row.missing_arcs_set() {
        if !missing.contains(&origin) && !missing.contains(&destination) {
            let destination = if destination > 0 {
                destination.to_string()
            } else {
                "exit".to_owned()
            };
            items.push((origin, format!("{origin}->{destination}")));
        }
    }
    items.sort();
    items
        .into_iter()
        .map(|(_, text)| text)
        .collect::<Vec<_>>()
        .join(", ")
}

fn nice_pair(start: i32, end: i32) -> String {
    if start == end {
        start.to_string()
    } else {
        format!("{start}-{end}")
    }
}

struct TerminalLine {
    name: String,
    counts: Vec<usize>,
    cover: String,
    percent: f64,
    missing: String,
}

/// Render Coverage.py 7.15.4's text summary report.
#[must_use]
pub fn render_terminal(
    report: &ReportDisplay<'_>,
    precision: usize,
    show_missing: bool,
    color: bool,
) -> String {
    let has_branches = report.has_arcs;
    let lines: Vec<TerminalLine> = report
        .rows
        .iter()
        .map(|row| {
            let mut counts = vec![row.statement_count(), row.missing_count()];
            if has_branches {
                counts.extend([row.branch_count(), row.partial_count()]);
            }
            TerminalLine {
                name: terminal_filename(row).into_owned(),
                counts,
                cover: display_percent(row.percent_value(), precision),
                percent: row.percent_value(),
                missing: if show_missing {
                    missing_formatted(row)
                } else {
                    String::new()
                },
            }
        })
        .collect();
    let totals = report.totals;
    let mut total_counts = vec![totals.statements, totals.statements - totals.covered];
    if has_branches {
        total_counts.extend([totals.branches, totals.partial_branches]);
    }
    let total = TerminalLine {
        name: "TOTAL".to_owned(),
        counts: total_counts,
        cover: display_percent(totals.percent_value(), precision),
        percent: totals.percent_value(),
        missing: String::new(),
    };

    let name_width = lines
        .iter()
        .map(|line| line.name.chars().count())
        .max()
        .unwrap_or(0)
        .max(5)
        + 1;
    let cover_width = lines
        .iter()
        .map(|line| line.cover.len() + 2)
        .fold((total.cover.len() + 2).max(" Cover".len()) + 1, usize::max);
    let mut header = format!("{:name_width$}{:>7}{:>7}", "Name", "Stmts", "Miss");
    if has_branches {
        write!(header, "{:>7}{:>7}", "Branch", "BrPart").expect("writing to String cannot fail");
    }
    write!(header, "{:>cover_width$}", "Cover").expect("writing to String cannot fail");
    if show_missing {
        write!(header, "{:>10}", "Missing").expect("writing to String cannot fail");
    }
    let rule = "-".repeat(header.chars().count());
    let mut output = String::new();
    if color {
        writeln!(output, "\u{1b}[1m{header}\u{1b}[0m").expect("writing to String cannot fail");
    } else {
        writeln!(output, "{header}").expect("writing to String cannot fail");
    }
    writeln!(output, "{rule}").expect("writing to String cannot fail");
    let write_line = |output: &mut String, line: &TerminalLine| {
        let mut text = format!("{:name_width$}", line.name);
        for count in &line.counts {
            write!(text, "{count:>7}").expect("writing to String cannot fail");
        }
        let cell = format!("{:>width$}%", line.cover, width = cover_width - 1);
        text.push_str(&colored_cell(&cell, line.percent, color));
        if show_missing {
            write!(text, "   {:9}", line.missing).expect("writing to String cannot fail");
        }
        writeln!(output, "{}", text.trim_end()).expect("writing to String cannot fail");
    };
    for line in &lines {
        write_line(&mut output, line);
    }
    if !lines.is_empty() {
        writeln!(output, "{rule}").expect("writing to String cannot fail");
    }
    write_line(&mut output, &total);
    write_skipped_notes(&mut output, report);
    output
}

/// Append the notes that name files hidden by `skip_covered` and `skip_empty`.
fn write_skipped_notes(output: &mut String, report: &ReportDisplay<'_>) {
    if report.skipped_covered > 0 {
        write!(
            output,
            "\n{} skipped due to complete coverage.\n",
            plural(report.skipped_covered, "file")
        )
        .expect("writing to String cannot fail");
    }
    if report.skipped_empty > 0 {
        write!(
            output,
            "\n{} skipped.\n",
            plural(report.skipped_empty, "empty file")
        )
        .expect("writing to String cannot fail");
    }
}

fn plural(count: usize, noun: &str) -> String {
    if count == 1 {
        format!("{count} {noun}")
    } else {
        format!("{count} {noun}s")
    }
}

/// Prefix the punctuation Markdown tables treat as syntax, as Coverage.py's `escape_markdown`.
fn escape_markdown(text: &str) -> String {
    let mut escaped = String::with_capacity(text.len());
    for character in text.chars() {
        if character.is_ascii_punctuation() && !matches!(character, '.' | ',' | '/' | '-') {
            escaped.push('\\');
        }
        escaped.push(character);
    }
    escaped
}

/// Column captions of the Markdown table, in Coverage.py's order.
fn markdown_header(has_branches: bool, show_missing: bool) -> Vec<&'static str> {
    let mut header = vec!["Name", "Stmts", "Miss"];
    if has_branches {
        header.extend(["Branch", "BrPart"]);
    }
    header.push("Cover");
    if show_missing {
        header.push("Missing");
    }
    header
}

/// Escaped cell text for one file, aligned with `markdown_header`.
fn markdown_cells(
    row: &ReportRow,
    has_branches: bool,
    precision: usize,
    show_missing: bool,
) -> Vec<String> {
    let mut cells = vec![
        escape_markdown(&terminal_filename(row)),
        row.statement_count().to_string(),
        row.missing_count().to_string(),
    ];
    if has_branches {
        cells.extend([
            row.branch_count().to_string(),
            row.partial_count().to_string(),
        ]);
    }
    cells.push(escape_markdown(&display_percent(
        row.percent_value(),
        precision,
    )));
    if show_missing {
        cells.push(escape_markdown(&missing_formatted(row)));
    }
    cells
}

/// Unformatted totals aligned with `markdown_header`; the missing cell is empty.
fn markdown_totals(
    totals: ReportCounts,
    has_branches: bool,
    precision: usize,
    show_missing: bool,
) -> Vec<String> {
    let mut cells = vec![
        "TOTAL".to_owned(),
        totals.statements.to_string(),
        (totals.statements - totals.covered).to_string(),
    ];
    if has_branches {
        cells.extend([
            totals.branches.to_string(),
            totals.partial_branches.to_string(),
        ]);
    }
    cells.push(display_percent(totals.percent_value(), precision));
    if show_missing {
        cells.push(String::new());
    }
    cells
}

/// Render Coverage.py 7.15.4's Markdown summary report.
#[must_use]
pub fn render_markdown(report: &ReportDisplay<'_>, precision: usize, show_missing: bool) -> String {
    let has_branches = report.has_arcs;
    let header = markdown_header(has_branches, show_missing);
    let table: Vec<Vec<String>> = report
        .rows
        .iter()
        .map(|row| markdown_cells(row, has_branches, precision, show_missing))
        .collect();
    let total = markdown_totals(report.totals, has_branches, precision, show_missing);
    let name_width = table
        .iter()
        .map(|cells| cells[0].chars().count())
        .max()
        .unwrap_or(0)
        .max("**TOTAL**".len())
        + 1;
    // Coverage.py sizes the cover column from the total only.
    let cover_column = if show_missing {
        header.len() - 2
    } else {
        header.len() - 1
    };
    let cover_width = (total[cover_column].chars().count() + 6).max(" Cover ".len());
    let cell = |column: &str, text: &str| match column {
        "Name" => format!("| {text:<name_width$}|"),
        "Cover" => format!("{text:>cover_width$} |"),
        "Missing" => format!("{text:>10} |"),
        _ => format!("{text:>9} |"),
    };

    let header_cells: Vec<String> = header.iter().map(|column| cell(column, column)).collect();
    let mut output = header_cells.concat();
    output.push('\n');
    // Each rule cell is as wide as its header cell; the leading pipe is shared.
    let rule: Vec<String> = header_cells
        .iter()
        .enumerate()
        .map(|(index, item)| {
            let width = item.chars().count() - 1;
            let marker = if index == 0 { "- |" } else { "-: |" };
            format!("{marker:->width$}")
        })
        .collect();
    writeln!(output, "|{}", rule.join(" ")).expect("writing to String cannot fail");
    for cells in &table {
        for (column, text) in header.iter().zip(cells) {
            if *column == "Cover" {
                let width = cover_width - 1;
                write!(output, "{text:>width$}% |").expect("writing to String cannot fail");
            } else {
                output.push_str(&cell(column, text));
            }
        }
        output.push('\n');
    }
    for (column, value) in header.iter().zip(&total) {
        let text = match (*column, value.as_str()) {
            (_, "") => String::new(),
            ("Cover", value) => format!(" **{value}%**"),
            (_, value) => format!(" **{value}**"),
        };
        if *column == "Name" {
            write!(output, "|{text:<name_width$} |").expect("writing to String cannot fail");
        } else {
            output.push_str(&cell(column, &text));
        }
    }
    output.push('\n');
    write_skipped_notes(&mut output, report);
    output
}

/// Render the total percentage followed by a newline.
#[must_use]
pub fn render_total(report: &ReportDisplay<'_>, precision: usize) -> String {
    format!(
        "{}\n",
        display_percent(report.totals.percent_value(), precision)
    )
}

struct JsonSummary {
    counts: ReportCounts,
    has_arcs: bool,
    precision: usize,
}

impl Serialize for JsonSummary {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        let counts = self.counts;
        let mut record = serializer.serialize_map(None)?;
        record.serialize_entry("covered_lines", &counts.covered)?;
        record.serialize_entry("num_statements", &counts.statements)?;
        record.serialize_entry("percent_covered", &counts.percent_value())?;
        record.serialize_entry(
            "percent_covered_display",
            &display_percent(counts.percent_value(), self.precision),
        )?;
        record.serialize_entry("missing_lines", &counts.missing)?;
        record.serialize_entry("excluded_lines", &counts.excluded)?;
        record.serialize_entry("percent_statements_covered", &counts.line_percent_value())?;
        record.serialize_entry(
            "percent_statements_covered_display",
            &display_percent(counts.line_percent_value(), self.precision),
        )?;
        if self.has_arcs {
            record.serialize_entry("num_branches", &counts.branches)?;
            record.serialize_entry("num_partial_branches", &counts.partial_branches)?;
            record.serialize_entry("covered_branches", &counts.covered_branches)?;
            record.serialize_entry("missing_branches", &counts.missing_branches)?;
            record.serialize_entry("percent_branches_covered", &counts.branch_percent_value())?;
            record.serialize_entry(
                "percent_branches_covered_display",
                &display_percent(counts.branch_percent_value(), self.precision),
            )?;
        }
        record.end()
    }
}

struct RegionArcIndex {
    possible: BTreeMap<i32, Vec<Arc>>,
    covered: BTreeMap<i32, Vec<Arc>>,
    executed: BTreeMap<i32, Vec<Arc>>,
}

impl RegionArcIndex {
    fn new(row: &ReportRow) -> Self {
        fn index(arcs: &BTreeSet<Arc>) -> BTreeMap<i32, Vec<Arc>> {
            let mut by_origin: BTreeMap<i32, Vec<Arc>> = BTreeMap::new();
            for &(start, end) in arcs {
                by_origin.entry(start).or_default().push((start, end));
            }
            by_origin
        }

        Self {
            possible: index(&row.possible_arcs),
            covered: index(&row.covered_arcs),
            executed: index(&row.executed_arcs),
        }
    }

    fn arcs_for_lines(by_origin: &BTreeMap<i32, Vec<Arc>>, lines: &BTreeSet<i32>) -> BTreeSet<Arc> {
        if lines.len() <= by_origin.len() {
            lines
                .iter()
                .flat_map(|line| by_origin.get(line).into_iter().flatten().copied())
                .collect()
        } else {
            by_origin
                .iter()
                .filter(|(line, _)| lines.contains(line))
                .flat_map(|(_, arcs)| arcs.iter().copied())
                .collect()
        }
    }
}

struct JsonSequence<I>(I);

impl<I> Serialize for JsonSequence<I>
where
    I: Clone + Iterator,
    I::Item: Serialize,
{
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        let mut sequence = serializer.serialize_seq(None)?;
        for value in self.0.clone() {
            sequence.serialize_element(&value)?;
        }
        sequence.end()
    }
}

struct JsonLines<'a> {
    values: &'a BTreeSet<i32>,
    selected: Option<&'a BTreeSet<i32>>,
    excluded: Option<&'a BTreeSet<i32>>,
}

impl Serialize for JsonLines<'_> {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        if let Some(selected) = self.selected {
            let retained = |line: &&i32| {
                self.excluded
                    .is_none_or(|excluded| !excluded.contains(line))
            };
            JsonSequence(self.values.intersection(selected).filter(retained)).serialize(serializer)
        } else if let Some(excluded) = self.excluded {
            JsonSequence(self.values.difference(excluded)).serialize(serializer)
        } else {
            self.values.serialize(serializer)
        }
    }
}

struct JsonRecord<'a> {
    row: &'a ReportRow,
    lines: Option<&'a BTreeSet<i32>>,
    possible_arcs: Cow<'a, BTreeSet<Arc>>,
    covered_arcs: Cow<'a, BTreeSet<Arc>>,
    executed_arcs: Cow<'a, BTreeSet<Arc>>,
    start_line: Option<i32>,
    show_contexts: bool,
    precision: usize,
}

impl<'a> JsonRecord<'a> {
    fn file(row: &'a ReportRow, show_contexts: bool, precision: usize) -> Self {
        Self {
            row,
            lines: None,
            possible_arcs: Cow::Borrowed(&row.possible_arcs),
            covered_arcs: Cow::Borrowed(&row.covered_arcs),
            executed_arcs: Cow::Borrowed(&row.executed_arcs),
            start_line: None,
            show_contexts,
            precision,
        }
    }

    fn region(
        row: &'a ReportRow,
        lines: &'a BTreeSet<i32>,
        start_line: i32,
        show_contexts: bool,
        precision: usize,
        arcs: Option<&RegionArcIndex>,
    ) -> Self {
        let origin_selected = |arc: &&Arc| lines.contains(&arc.0);
        Self {
            row,
            lines: Some(lines),
            possible_arcs: Cow::Owned(arcs.map_or_else(
                || {
                    row.possible_arcs
                        .iter()
                        .filter(origin_selected)
                        .copied()
                        .collect()
                },
                |arcs| RegionArcIndex::arcs_for_lines(&arcs.possible, lines),
            )),
            covered_arcs: Cow::Owned(arcs.map_or_else(
                || {
                    row.covered_arcs
                        .iter()
                        .filter(origin_selected)
                        .copied()
                        .collect()
                },
                |arcs| RegionArcIndex::arcs_for_lines(&arcs.covered, lines),
            )),
            executed_arcs: Cow::Owned(arcs.map_or_else(
                || {
                    row.executed_arcs
                        .iter()
                        .filter(origin_selected)
                        .copied()
                        .collect()
                },
                |arcs| RegionArcIndex::arcs_for_lines(&arcs.executed, lines),
            )),
            start_line: Some(start_line),
            show_contexts,
            precision,
        }
    }

    fn counts(&self) -> ReportCounts {
        let Some(lines) = self.lines else {
            return ReportCounts::from(self.row);
        };
        let statements = self.row.executable.intersection(lines).count();
        let covered = self.row.covered.intersection(lines).count();
        let branches = self.possible_arcs.len();
        let covered_branches = self.covered_arcs.len();
        ReportCounts {
            statements,
            covered,
            missing: statements - covered,
            excluded: self.row.excluded.intersection(lines).count(),
            branches,
            covered_branches,
            missing_branches: branches - covered_branches,
            partial_branches: partial_branch_count(
                &self.possible_arcs,
                &self.covered_arcs,
                &self.row.executable,
                &self.row.covered,
            ),
        }
    }

    fn serialize_entries<M: SerializeMap>(&self, record: &mut M) -> Result<(), M::Error> {
        record.serialize_entry(
            "executed_lines",
            &JsonLines {
                values: &self.row.covered,
                selected: self.lines,
                excluded: None,
            },
        )?;
        record.serialize_entry(
            "summary",
            &JsonSummary {
                counts: self.counts(),
                has_arcs: self.row.has_arcs,
                precision: self.precision,
            },
        )?;
        record.serialize_entry(
            "missing_lines",
            &JsonLines {
                values: &self.row.executable,
                selected: self.lines,
                excluded: Some(&self.row.covered),
            },
        )?;
        record.serialize_entry(
            "excluded_lines",
            &JsonLines {
                values: &self.row.excluded,
                selected: self.lines,
                excluded: None,
            },
        )?;
        if let Some(start_line) = self.start_line {
            record.serialize_entry("start_line", &start_line)?;
        }
        if self.row.has_arcs {
            record.serialize_entry("executed_branches", self.executed_arcs.as_ref())?;
            record.serialize_entry(
                "missing_branches",
                &JsonSequence(self.possible_arcs.difference(&self.covered_arcs)),
            )?;
        }
        if self.show_contexts {
            record.serialize_entry("contexts", &self.row.contexts_by_line)?;
        }
        Ok(())
    }
}

impl Serialize for JsonRecord<'_> {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        let mut record = serializer.serialize_map(None)?;
        self.serialize_entries(&mut record)?;
        record.end()
    }
}

struct JsonRegions<'a> {
    row: &'a ReportRow,
    kind: &'static str,
    show_contexts: bool,
    precision: usize,
    arcs: Option<&'a RegionArcIndex>,
}

impl Serialize for JsonRegions<'_> {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        let mut outside: BTreeSet<i32> = (1..=self.row.source_line_count).collect();
        let mut positions = BTreeMap::new();
        let mut regions: Vec<&CodeRegion> = Vec::new();
        for region in self
            .row
            .regions
            .iter()
            .filter(|region| region.kind == self.kind)
        {
            for line in &region.lines {
                outside.remove(line);
            }
            let position = *positions.entry(region.name.as_str()).or_insert_with(|| {
                regions.push(region);
                regions.len() - 1
            });
            regions[position] = region;
        }
        let outside_record = JsonRecord::region(
            self.row,
            &outside,
            outside.first().copied().unwrap_or(1),
            self.show_contexts,
            self.precision,
            self.arcs,
        );
        let mut records = serializer.serialize_map(None)?;
        for region in &regions {
            if region.name.is_empty() {
                records.serialize_entry("", &outside_record)?;
            } else {
                records.serialize_entry(
                    &region.name,
                    &JsonRecord::region(
                        self.row,
                        &region.lines,
                        region.start,
                        self.show_contexts,
                        self.precision,
                        self.arcs,
                    ),
                )?;
            }
        }
        if !positions.contains_key("") {
            records.serialize_entry("", &outside_record)?;
        }
        records.end()
    }
}

struct JsonFile<'a> {
    row: &'a ReportRow,
    show_contexts: bool,
    precision: usize,
}

impl Serialize for JsonFile<'_> {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        let arcs = (!self.row.regions.is_empty()).then(|| RegionArcIndex::new(self.row));
        let mut record = serializer.serialize_map(None)?;
        JsonRecord::file(self.row, self.show_contexts, self.precision)
            .serialize_entries(&mut record)?;
        for kind in ["function", "class"] {
            record.serialize_entry(
                if kind == "function" {
                    "functions"
                } else {
                    "classes"
                },
                &JsonRegions {
                    row: self.row,
                    kind,
                    show_contexts: self.show_contexts,
                    precision: self.precision,
                    arcs: arcs.as_ref(),
                },
            )?;
        }
        record.end()
    }
}

struct JsonFiles<'a> {
    rows: &'a [&'a ReportRow],
    show_contexts: bool,
    precision: usize,
}

impl Serialize for JsonFiles<'_> {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        let mut positions = BTreeMap::new();
        let mut rows: Vec<&ReportRow> = Vec::new();
        for &row in self.rows {
            let filename = row
                .report_file_paths
                .as_ref()
                .map_or(row.path.as_str(), |paths| paths.relative.as_str());
            let position = *positions.entry(filename).or_insert_with(|| {
                rows.push(row);
                rows.len() - 1
            });
            rows[position] = row;
        }
        let mut files = serializer.serialize_map(None)?;
        for row in rows {
            let filename = row
                .report_file_paths
                .as_ref()
                .map_or(row.path.as_str(), |paths| paths.relative.as_str());
            files.serialize_entry(
                filename,
                &JsonFile {
                    row,
                    show_contexts: self.show_contexts,
                    precision: self.precision,
                },
            )?;
        }
        files.end()
    }
}

struct JsonReport<'a> {
    metadata: Value,
    files: JsonFiles<'a>,
    totals: JsonSummary,
}

impl Serialize for JsonReport<'_> {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        let mut report = serializer.serialize_map(Some(3))?;
        report.serialize_entry("meta", &self.metadata)?;
        report.serialize_entry("files", &self.files)?;
        report.serialize_entry("totals", &self.totals)?;
        report.end()
    }
}

/// Render the shared report model as JSON.
///
/// # Errors
/// Returns a serialization error if a report value cannot be encoded.
pub fn render_json(
    rows: &[&ReportRow],
    pretty: bool,
    show_contexts: bool,
    precision: usize,
) -> ReportResult<String> {
    let has_arcs = rows.iter().any(|row| row.has_arcs);
    let value = JsonReport {
        metadata: json!({
            "format": 3,
            "version": format!("vfy {}", env!("CARGO_PKG_VERSION")),
            "timestamp": jiff::Timestamp::now().to_string(),
            "branch_coverage": has_arcs,
            "show_contexts": show_contexts,
        }),
        files: JsonFiles {
            rows,
            show_contexts,
            precision,
        },
        totals: JsonSummary {
            counts: total_counts(rows),
            has_arcs,
            precision,
        },
    };
    let mut rendered = if pretty {
        serde_json::to_string_pretty(&value)
    } else {
        serde_json::to_string(&value)
    }
    .map_err(ReportError::Serialization)?;
    rendered.push('\n');
    Ok(rendered)
}

/// Render LCOV records, one per file.
#[must_use]
pub fn render_lcov(rows: &[&ReportRow], unicode_version: UnicodeVersion) -> String {
    let mut records = String::new();
    for row in rows {
        writeln!(
            records,
            "TN:\nSF:{}",
            lcov_field(row.report_filename(), unicode_version)
        )
        .expect("writing to String cannot fail");
        let missing = row.missing_set();
        for line in &row.executable {
            writeln!(
                records,
                "DA:{line},{}",
                usize::from(!missing.contains(line))
            )
            .expect("writing to String cannot fail");
        }
        if row.statement_count() > 0 {
            writeln!(
                records,
                "LF:{}\nLH:{}",
                row.statement_count(),
                row.covered_count()
            )
            .expect("writing to String cannot fail");
        }
        lcov_functions(&mut records, row, unicode_version);
        lcov_arcs(&mut records, row, unicode_version);
        writeln!(records, "end_of_record").expect("writing to String cannot fail");
    }
    records
}

fn lcov_functions(records: &mut String, row: &ReportRow, unicode_version: UnicodeVersion) {
    let mut functions: Vec<_> = row
        .regions
        .iter()
        .filter(|region| region.kind == "function")
        .filter_map(|region| {
            let first = *region.lines.first()?;
            let last = *region.lines.last()?;
            Some((region.start.min(first), region.start.max(last), region))
        })
        .collect();
    if functions.is_empty() {
        return;
    }
    functions.sort_by(|left, right| {
        (left.0, left.1, left.2.name.as_str()).cmp(&(right.0, right.1, right.2.name.as_str()))
    });
    let mut found = 0;
    let mut hit = 0;
    for (first, last, region) in functions {
        if !region
            .lines
            .iter()
            .any(|line| row.executable.contains(line))
        {
            continue;
        }
        found += 1;
        let covered = region.lines.iter().any(|line| row.covered.contains(line));
        hit += usize::from(covered);
        let name = lcov_field(&region.name, unicode_version);
        writeln!(
            records,
            "FN:{first},{last},{name}\nFNDA:{},{name}",
            usize::from(covered)
        )
        .expect("writing to String cannot fail");
    }
    writeln!(records, "FNF:{found}\nFNH:{hit}").expect("writing to String cannot fail");
}

fn lcov_arcs(records: &mut String, row: &ReportRow, unicode_version: UnicodeVersion) {
    let mut by_origin: BTreeMap<i32, Vec<i32>> = BTreeMap::new();
    for (origin, destination) in &row.possible_arcs {
        by_origin.entry(*origin).or_default().push(*destination);
    }
    for (origin, mut destinations) in by_origin {
        destinations.sort_by_key(|destination| (*destination < 0, *destination));
        let origin_taken = destinations
            .iter()
            .any(|destination| row.covered_arcs.contains(&(origin, *destination)));
        for destination in destinations {
            let taken = if row.covered_arcs.contains(&(origin, destination)) {
                "1"
            } else if origin_taken {
                "0"
            } else {
                "-"
            };
            let description = if destination >= 0 {
                format!("jump to line {destination}")
            } else {
                match row.arc_exit_descriptions.get(&(origin, destination)) {
                    Some(crate::analysis::ExitDescription::Module) => "exit the module".to_owned(),
                    Some(crate::analysis::ExitDescription::ReturnFunction(name)) => {
                        format!("return from function '{name}'")
                    }
                    Some(crate::analysis::ExitDescription::ExitClass(name)) => {
                        format!("exit class '{name}'")
                    }
                    None => format!("jump to line {destination}"),
                }
            };
            writeln!(
                records,
                "BRDA:{origin},0,{},{}",
                lcov_field(&description, unicode_version),
                taken
            )
            .expect("writing to String cannot fail");
        }
    }
    if !row.possible_arcs.is_empty() {
        writeln!(
            records,
            "BRF:{}\nBRH:{}",
            row.branch_count(),
            row.covered_branch_count()
        )
        .expect("writing to String cannot fail");
    }
}

fn lcov_field(text: &str, unicode_version: UnicodeVersion) -> String {
    text.chars()
        .map(|character| {
            if unicode::is_printable(character, unicode_version) {
                character
            } else {
                '?'
            }
        })
        .collect()
}

/// Annotate source lines with their coverage state.
///
/// # Errors
/// Returns an output error when the source cannot be read.
pub fn render_annotation(row: &ReportRow, source: Option<&str>) -> ReportResult<String> {
    let loaded;
    let source = if let Some(source) = source {
        source
    } else if let Some(source) = row.source_override.as_deref() {
        source
    } else {
        loaded = crate::read_python_source(&row.path).map_err(|error| {
            ReportError::Output(format!(
                "failed to read source for annotation {}: {error}",
                row.path
            ))
        })?;
        &loaded
    };
    let missing = row.missing_set();
    Ok(source
        .split_inclusive('\n')
        .enumerate()
        .fold(String::new(), |mut output, (index, text)| {
            let line = i32::try_from(index + 1).unwrap_or(i32::MAX);
            let prefix = if row.excluded.contains(&line) {
                "- "
            } else if missing.contains(&line) {
                "! "
            } else if row.executable.contains(&line) {
                "> "
            } else {
                "  "
            };
            write!(output, "{prefix}{text}").expect("writing to String cannot fail");
            output
        }))
}

fn atomic_write(path: &Path, content: impl AsRef<[u8]>) -> Result<(), String> {
    crate::atomic_write(path, content.as_ref()).map_err(|error| error.to_string())
}

fn output_error(path: &Path, error: &str) -> ReportError {
    ReportError::Output(format!(
        "failed to write report output {}: {error}",
        path.display()
    ))
}

/// Replace a report file atomically.
///
/// # Errors
/// Returns an output error if staging or replacement fails; the previous file remains valid.
pub fn write_report_output(content: &str, destination: &str) -> ReportResult<()> {
    let path = Path::new(destination);
    atomic_write(path, content).map_err(|error| output_error(path, &error))
}

fn atomic_append(path: &Path, content: &str) -> Result<(), String> {
    let mut combined = match std::fs::read(path) {
        Ok(existing) => existing,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Vec::new(),
        Err(error) => return Err(error.to_string()),
    };
    combined.extend_from_slice(content.as_bytes());
    atomic_write(path, &combined)
}

/// Append bytes using an atomic replacement.
///
/// # Errors
/// Returns an output error if existing bytes cannot be read or replacement fails.
pub fn append_report_output(content: &str, destination: &str) -> ReportResult<()> {
    let path = Path::new(destination);
    atomic_append(path, content).map_err(|error| output_error(path, &error))
}

/// Select detail pages while retaining all files in the overall total.
#[derive(Clone, Copy, Default)]
pub struct HtmlSelection {
    pub skip_covered: bool,
    pub skip_empty: bool,
}

/// Render HTML and replace each generated file atomically.
///
/// # Errors
/// Returns an output error for unavailable source, CSS, or a failed file replacement.
pub fn write_html_report(
    rows: &[&ReportRow],
    directory: &str,
    title: &str,
    precision: usize,
    show_contexts: bool,
    extra_css: Option<&str>,
    selection: HtmlSelection,
) -> ReportResult<()> {
    let root = Path::new(directory);
    let stylesheet = extra_css
        .map(|path| {
            std::fs::read(path).map_err(|error| {
                ReportError::Output(format!("failed to read HTML extra CSS {path:?}: {error}"))
            })
        })
        .transpose()?;
    let outputs = render_html_options(
        rows,
        &HtmlOptions {
            title,
            precision,
            show_contexts,
            extra_css: stylesheet.is_some(),
            selection,
        },
    )?;
    if let Some(stylesheet) = stylesheet {
        let path = root.join("extra.css");
        atomic_write(&path, stylesheet).map_err(|error| output_error(&path, &error))?;
    }
    for (filename, content) in outputs {
        let path = root.join(filename);
        atomic_write(&path, &content).map_err(|error| output_error(&path, &error))?;
    }
    Ok(())
}

/// Write per-source annotation files.
///
/// # Errors
/// Returns an output error for unavailable source or a failed file replacement.
pub fn write_annotation_report(
    rows: &[&ReportRow],
    directory: Option<&str>,
) -> ReportResult<usize> {
    let root = directory.map(PathBuf::from);
    let outputs = rows
        .iter()
        .map(|row| {
            let source = Path::new(&row.path);
            let destination = if let Some(root) = &root {
                let relative_filename = row.report_file_paths.as_ref().map_or_else(
                    || super::relative_report_filename(&row.path),
                    |paths| paths.relative.clone(),
                );
                let flat_name = flat_rootname(&relative_filename);
                let name = flat_name.strip_suffix("_py").map_or_else(
                    || format!("{flat_name},cover"),
                    |stem| format!("{stem}.py,cover"),
                );
                root.join(name)
            } else {
                let mut destination = source.as_os_str().to_os_string();
                destination.push(",cover");
                PathBuf::from(destination)
            };
            Ok((destination, render_annotation(row, None)?))
        })
        .collect::<ReportResult<Vec<_>>>()?;
    let count = outputs.len();
    for (destination, content) in outputs {
        atomic_write(&destination, &content).map_err(|error| output_error(&destination, &error))?;
    }
    Ok(count)
}

fn flat_rootname(filename: &str) -> String {
    // Coverage.py uses ntpath.split even on POSIX, so both separators matter.
    let split = filename.rfind(['/', '\\']).map_or_else(
        || usize::from(filename.as_bytes().get(1) == Some(&b':')) * 2,
        |index| index + 1,
    );
    let (directory, basename) = filename.split_at(split);
    let directory = if directory
        .chars()
        .any(|character| !matches!(character, '/' | '\\'))
    {
        directory.trim_end_matches(['/', '\\'])
    } else {
        directory
    };
    let mut name = String::new();
    if !directory.is_empty() {
        let digest = Sha3_256::digest(directory.as_bytes());
        name.push_str("z_");
        for byte in &digest[..8] {
            write!(name, "{byte:02x}").expect("writing to String cannot fail");
        }
        name.push('_');
    }
    name.push_str(&basename.replace('.', "_"));
    name
}

fn xml_escape(value: &str) -> String {
    value
        .replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&apos;")
}

const HTML_STYLE: &str = ":root { color-scheme: light dark; font-family: ui-sans-serif, system-ui, sans-serif; }\n\
body { margin: 2rem auto; max-width: 90rem; padding: 0 1rem; }\n\
table { border-collapse: collapse; width: 100%; }\n\
th, td { border-bottom: 1px solid #8885; padding: .45rem .6rem; text-align: right; }\n\
th:first-child, td:first-child { text-align: left; }\n\
pre { line-height: 1.45; overflow-x: auto; }\n\
.covered { background: #2d8a4e22; }\n\
.missing { background: #d13c3c28; }\n\
.excluded { color: #888; }\n\
.number { color: #888; display: inline-block; text-align: right; width: 4rem; margin-right: 1rem; }\n";

/// Render the HTML file set without writing it.
///
/// # Errors
/// Returns an output error when a required source file cannot be read.
pub fn render_html(rows: &[&ReportRow], title: &str) -> ReportResult<Vec<(String, String)>> {
    render_html_options(
        rows,
        &HtmlOptions {
            title,
            precision: 1,
            show_contexts: false,
            extra_css: false,
            selection: HtmlSelection::default(),
        },
    )
}

struct HtmlOptions<'a> {
    title: &'a str,
    precision: usize,
    show_contexts: bool,
    extra_css: bool,
    selection: HtmlSelection,
}

fn render_html_options(
    rows: &[&ReportRow],
    options: &HtmlOptions<'_>,
) -> ReportResult<Vec<(String, String)>> {
    let title = options.title;
    let precision = options.precision;
    let mut files = vec![("style.css".to_owned(), HTML_STYLE.to_owned())];
    let extra_link = if options.extra_css {
        "<link rel=\"stylesheet\" href=\"extra.css\">"
    } else {
        ""
    };
    let mut table_rows = String::new();
    for index in super::select_report_indices(
        rows,
        options.selection.skip_covered,
        options.selection.skip_empty,
        None,
    )? {
        let row = rows[index];
        let digest = Sha256::digest(row.path.as_bytes());
        let mut token = String::with_capacity(16);
        for byte in &digest[..8] {
            write!(token, "{byte:02x}").expect("writing to String cannot fail");
        }
        let detail_name = format!("z_{token}.html");
        write!(
            table_rows,
            "<tr><td><a href=\"{}\">{}</a></td><td>{}</td><td>{}</td><td>{}</td><td>{}%</td></tr>",
            detail_name,
            html_escape(row.report_filename()),
            row.statement_count(),
            row.missing_count(),
            row.branch_count(),
            display_percent(row.percent_value(), precision),
        )
        .expect("writing to String cannot fail");
        let source = match &row.source_override {
            Some(source) => source.clone(),
            None => crate::read_python_source(&row.path).map_err(|error| {
                ReportError::Output(format!(
                    "failed to read source for HTML report {}: {error}",
                    row.path
                ))
            })?,
        };
        let missing = row.missing_set();
        let mut source_lines = String::new();
        for (index, text) in source.lines().enumerate() {
            let line = i32::try_from(index + 1).unwrap_or(i32::MAX);
            let state = if row.excluded.contains(&line) {
                "excluded"
            } else if missing.contains(&line) {
                "missing"
            } else if row.executable.contains(&line) {
                "covered"
            } else {
                "plain"
            };
            if index > 0 {
                source_lines.push('\n');
            }
            write!(
                source_lines,
                "<span class=\"{state}\"><span class=\"number\">{line}</span>{}</span>",
                html_escape(text),
            )
            .expect("writing to String cannot fail");
            if options.show_contexts
                && let Some(contexts) = row.contexts_by_line.get(&line)
            {
                write!(
                    source_lines,
                    "<span class=\"contexts\">{}</span>",
                    html_escape(&contexts.join(", "))
                )
                .expect("writing to String cannot fail");
            }
        }
        let detail = format!(
            "<!doctype html><html><head><meta charset=\"utf-8\"><title>{} — {}</title><link rel=\"stylesheet\" href=\"style.css\">{extra_link}</head><body><p><a href=\"index.html\">Index</a></p><h1>{}</h1><p>{}% covered</p><pre>{}</pre></body></html>\n",
            html_escape(row.report_filename()),
            html_escape(title),
            html_escape(row.report_filename()),
            display_percent(row.percent_value(), precision),
            source_lines,
        );
        files.push((detail_name, detail));
    }
    let total = total_counts(rows);
    let index = format!(
        "<!doctype html><html><head><meta charset=\"utf-8\"><title>{}</title><link rel=\"stylesheet\" href=\"style.css\">{extra_link}</head><body><h1>{}</h1><p>Total coverage: {}%</p><table><thead><tr><th>File</th><th>Statements</th><th>Missing</th><th>Branches</th><th>Coverage</th></tr></thead><tbody>{}</tbody></table></body></html>\n",
        html_escape(title),
        html_escape(title),
        display_percent(total.percent_value(), precision),
        table_rows,
    );
    files.push(("index.html".to_owned(), index));
    Ok(files)
}

#[expect(
    clippy::cast_precision_loss,
    reason = "XML display matches Coverage.py's floating-point hit/total division; counters remain integers"
)]
fn xml_rate(hit: usize, total: usize) -> String {
    if total == 0 {
        return "1".to_owned();
    }
    let value = hit as f64 / total as f64;
    // Rounded scientific notation determines .4g's exponent before choosing
    // fixed notation, including rounding across a power-of-ten boundary.
    let scientific = format!("{value:.3e}");
    let Some((digits, exponent)) = scientific.split_once('e') else {
        return scientific;
    };
    let Ok(exponent) = exponent.parse::<i32>() else {
        return scientific;
    };
    if !(-4..4).contains(&exponent) {
        let digits = digits.trim_end_matches('0').trim_end_matches('.');
        return format!("{digits}e{exponent:+03}");
    }
    let Ok(precision) = usize::try_from(3 - exponent) else {
        return scientific;
    };
    let fixed = format!("{value:.precision$}");
    if precision == 0 {
        fixed
    } else {
        fixed.trim_end_matches('0').trim_end_matches('.').to_owned()
    }
}

fn xml_branch_rate(counts: ReportCounts, has_arcs: bool) -> String {
    if has_arcs {
        xml_rate(counts.covered_branches, counts.branches)
    } else {
        "0".to_owned()
    }
}

fn xml_sources(source_paths: &BTreeSet<String>) -> String {
    let mut sources = String::new();
    let mut paths = source_paths.iter().collect::<Vec<_>>();
    paths.sort_by(|left, right| human_cmp(left, right));
    for path in paths {
        writeln!(sources, "    <source>{}</source>", xml_escape(path))
            .expect("writing to String cannot fail");
    }
    sources
}

fn human_part(value: &str) -> (&str, &str, &str) {
    let number_start = value
        .find(|character: char| character.is_ascii_digit())
        .unwrap_or(value.len());
    let (text, remainder) = value.split_at(number_start);
    let number_end = remainder
        .find(|character: char| !character.is_ascii_digit())
        .unwrap_or(remainder.len());
    let (number, remainder) = remainder.split_at(number_end);
    (text, number, remainder)
}

fn human_number_cmp(left: &str, right: &str) -> std::cmp::Ordering {
    let left_significant = left.trim_start_matches('0');
    let right_significant = right.trim_start_matches('0');
    let left_significant = if left_significant.is_empty() {
        "0"
    } else {
        left_significant
    };
    let right_significant = if right_significant.is_empty() {
        "0"
    } else {
        right_significant
    };
    left_significant
        .len()
        .cmp(&right_significant.len())
        .then_with(|| left_significant.cmp(right_significant))
}

/// Compare ASCII numeric filename components without bounded integer parsing.
pub(super) fn human_cmp(left: &str, right: &str) -> std::cmp::Ordering {
    let (mut left_rest, mut right_rest) = (left, right);
    loop {
        let (left_text, left_number, left_suffix) = human_part(left_rest);
        let (right_text, right_number, right_suffix) = human_part(right_rest);
        let ordering = left_text.cmp(right_text);
        if ordering != std::cmp::Ordering::Equal {
            return ordering;
        }
        match (left_number.is_empty(), right_number.is_empty()) {
            (true, true) => return left.cmp(right),
            (true, false) => return std::cmp::Ordering::Less,
            (false, true) => return std::cmp::Ordering::Greater,
            (false, false) => {}
        }
        let ordering = human_number_cmp(left_number, right_number);
        if ordering != std::cmp::Ordering::Equal {
            return ordering;
        }
        (left_rest, right_rest) = (left_suffix, right_suffix);
    }
}

fn xml_source_roots(
    source_roots: &[String],
    relative_files: bool,
) -> (Vec<String>, BTreeSet<String>) {
    let mut roots = Vec::new();
    let mut source_paths = BTreeSet::new();
    for configured in source_roots {
        let path = Path::new(configured);
        if !path.exists() {
            continue;
        }
        let source = if relative_files {
            configured.trim_end_matches(['/', '\\']).to_owned()
        } else {
            crate::path::absolute_path(path)
                .to_string_lossy()
                .into_owned()
        };
        let normalized = source.replace('\\', "/");
        source_paths.insert(source);
        roots.push(normalized);
    }
    // Nested roots should select the most specific base before a containing root.
    roots.sort_by(|left: &String, right: &String| {
        right.len().cmp(&left.len()).then_with(|| left.cmp(right))
    });
    (roots, source_paths)
}

fn xml_relative_filename(filename: &str, current_dir: &Path) -> String {
    Path::new(filename).strip_prefix(current_dir).map_or_else(
        |_| filename.replace('\\', "/"),
        |relative| relative.to_string_lossy().replace('\\', "/"),
    )
}

fn xml_source_root_relative_filename(
    row: &ReportRow,
    roots: &[String],
    current_dir: &Path,
    relative_files: bool,
    source_paths: &mut BTreeSet<String>,
) -> String {
    // Coverage.py canonicalizes the reporter filename, but derives its fallback
    // relative name from the original data path.
    let reporter_filename = if relative_files {
        row.path.clone()
    } else {
        crate::path::absolute_path(Path::new(&row.path))
            .to_string_lossy()
            .into_owned()
    };
    let filename = reporter_filename.replace('\\', "/");
    for root in roots {
        let prefix = format!("{root}/");
        if let Some(relative) = filename.strip_prefix(&prefix) {
            return relative.to_owned();
        }
    }

    let relative = xml_relative_filename(row.report_filename(), current_dir);
    let prefix_length = reporter_filename
        .chars()
        .count()
        .saturating_sub(relative.chars().count());
    let source = reporter_filename
        .chars()
        .take(prefix_length)
        .collect::<String>()
        .trim_end_matches(['/', '\\'])
        .to_owned();
    source_paths.insert(source);
    relative
}

fn render_xml_lines(output: &mut String, row: &ReportRow) {
    let mut possible: BTreeMap<i32, BTreeSet<i32>> = BTreeMap::new();
    let mut covered: BTreeMap<i32, BTreeSet<i32>> = BTreeMap::new();
    for (origin, destination) in &row.possible_arcs {
        possible.entry(*origin).or_default().insert(*destination);
    }
    for (origin, destination) in &row.covered_arcs {
        covered.entry(*origin).or_default().insert(*destination);
    }
    let missing = row.missing_set();
    for line in &row.executable {
        if let Some(destinations) = possible.get(line) {
            let count = covered.get(line).map_or(0, BTreeSet::len);
            let missing_destinations = destinations
                .iter()
                .filter(|destination| {
                    !covered
                        .get(line)
                        .is_some_and(|arcs| arcs.contains(destination))
                })
                .map(|destination| {
                    if *destination < 0 {
                        "exit".to_owned()
                    } else {
                        destination.to_string()
                    }
                })
                .collect::<Vec<_>>();
            let missing_attribute = if missing_destinations.is_empty() {
                String::new()
            } else {
                format!(" missing-branches=\"{}\"", missing_destinations.join(","))
            };
            writeln!(
                output,
                "            <line number=\"{line}\" hits=\"{}\" branch=\"true\" condition-coverage=\"{}% ({}/{})\"{missing_attribute} />",
                usize::from(!missing.contains(line)),
                100 * count / destinations.len(),
                count,
                destinations.len(),
            )
            .expect("writing to String cannot fail");
        } else {
            writeln!(
                output,
                "            <line number=\"{line}\" hits=\"{}\" />",
                usize::from(!missing.contains(line)),
            )
            .expect("writing to String cannot fail");
        }
    }
}

/// Render Cobertura XML with default path options.
#[must_use]
pub fn render_xml(rows: &[&ReportRow], package_depth: Option<usize>) -> String {
    render_xml_with_options(
        rows,
        XmlOptions {
            package_depth: package_depth.map(|depth| i64::try_from(depth).unwrap_or(i64::MAX)),
            source_roots: &[],
            relative_files: false,
        },
    )
}

/// Options that affect XML path presentation, based on Coverage.py's run configuration.
#[derive(Clone, Copy)]
pub struct XmlOptions<'a> {
    /// Maximum directory depth represented as XML packages.
    pub package_depth: Option<i64>,
    /// Configured source roots used to strip report paths.
    pub source_roots: &'a [String],
    /// Whether Coverage.py keeps configured source roots relative.
    pub relative_files: bool,
}

/// Render Cobertura XML using `options` for package depth and source-root paths.
#[must_use]
pub fn render_xml_with_options(rows: &[&ReportRow], options: XmlOptions<'_>) -> String {
    let total = total_counts(rows);
    let has_arcs = rows.iter().any(|row| row.has_arcs);
    let current_dir = std::env::current_dir().unwrap_or_default();
    let (source_roots, mut source_paths) =
        xml_source_roots(options.source_roots, options.relative_files);
    let mut groups: BTreeMap<String, Vec<XmlFile<'_>>> = BTreeMap::new();
    for row in rows {
        let relative = xml_source_root_relative_filename(
            row,
            &source_roots,
            &current_dir,
            options.relative_files,
            &mut source_paths,
        );
        let (package, class_name) = xml_file_location(&relative, options.package_depth);
        groups.entry(package).or_default().push(XmlFile {
            relative_name: relative,
            class_name,
            row,
        });
    }
    let sources = xml_sources(&source_paths);
    let timestamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |duration| duration.as_millis());
    let mut output = format!(
        "<?xml version=\"1.0\" ?>\n<coverage version=\"vfy\" line-rate=\"{}\" branch-rate=\"{}\" lines-covered=\"{}\" lines-valid=\"{}\" branches-covered=\"{}\" branches-valid=\"{}\" timestamp=\"{}\" complexity=\"0\">\n  <sources>\n{}  </sources>\n  <packages>\n",
        xml_rate(total.covered, total.statements),
        xml_branch_rate(total, has_arcs),
        total.covered,
        total.statements,
        total.covered_branches,
        total.branches,
        timestamp,
        sources,
    );
    let mut groups = groups.into_iter().collect::<Vec<_>>();
    groups.sort_by(|(left, _), (right, _)| human_cmp(left, right));
    for (package, mut package_rows) in groups {
        package_rows.sort_by(|left, right| human_cmp(&left.relative_name, &right.relative_name));
        let counts: ReportCounts = package_rows
            .iter()
            .map(|file| ReportCounts::from(file.row))
            .sum();
        writeln!(
            output,
            "    <package name=\"{}\" line-rate=\"{}\" branch-rate=\"{}\" complexity=\"0\">\n      <classes>",
            xml_escape(&package),
            xml_rate(counts.covered, counts.statements),
            xml_branch_rate(counts, has_arcs),
        )
        .expect("writing to String cannot fail");
        for file in package_rows {
            let counts = ReportCounts::from(file.row);
            writeln!(
                output,
                "        <class name=\"{}\" filename=\"{}\" line-rate=\"{}\" branch-rate=\"{}\" complexity=\"0\">\n          <methods/>\n          <lines>",
                xml_escape(&file.class_name),
                xml_escape(&file.relative_name),
                xml_rate(counts.covered, counts.statements),
                xml_branch_rate(counts, has_arcs),
            )
            .expect("writing to String cannot fail");
            render_xml_lines(&mut output, file.row);
            output.push_str("          </lines>\n        </class>\n");
        }
        output.push_str("      </classes>\n    </package>\n");
    }
    output.push_str("  </packages>\n</coverage>\n");
    output
}

fn xml_package_depth(directory_count: usize, package_depth: Option<i64>) -> usize {
    match package_depth {
        None => directory_count.min(99),
        Some(depth) if depth >= 0 => {
            directory_count.min(usize::try_from(depth).unwrap_or(usize::MAX))
        }
        Some(depth) => directory_count
            .saturating_sub(usize::try_from(depth.unsigned_abs()).unwrap_or(usize::MAX)),
    }
}

struct XmlFile<'a> {
    relative_name: String,
    class_name: String,
    row: &'a ReportRow,
}

fn xml_file_location(relative_name: &str, package_depth: Option<i64>) -> (String, String) {
    let path = Path::new(relative_name);
    let dirname = path
        .parent()
        .unwrap_or_else(|| Path::new(""))
        .to_string_lossy()
        .replace('\\', "/");
    let dirname = if dirname.is_empty() { "." } else { &dirname };
    let directories = dirname.split('/').collect::<Vec<_>>();
    let depth = xml_package_depth(directories.len(), package_depth);
    let package = directories
        .iter()
        .take(depth)
        .copied()
        .collect::<Vec<_>>()
        .join(".");
    let package_dir = directories
        .iter()
        .take(depth)
        .copied()
        .collect::<Vec<_>>()
        .join("/");
    let class_name = if package_dir.is_empty() || package_dir == "." {
        relative_name.to_owned()
    } else {
        path.strip_prefix(Path::new(&package_dir))
            .unwrap_or(path)
            .to_string_lossy()
            .into_owned()
    };
    (package, class_name)
}
