//! Fixed-work semantic reporting for process CPU and peak RSS measurements.

use std::env;
use std::hint::black_box;
use std::process::ExitCode;

#[expect(
    dead_code,
    reason = "Criterion entry points in the shared fixtures are unused by the fixed-work binary"
)]
#[path = "engine/semantic_reports.rs"]
mod semantic_reports;

fn main() -> ExitCode {
    let mut arguments = env::args().skip(1);
    let Some(case) = arguments.next() else {
        eprintln!("usage: semantic_mcdc_resources CASE REPETITIONS");
        return ExitCode::FAILURE;
    };
    let Some(repetitions) = arguments
        .next()
        .and_then(|value| value.parse::<usize>().ok())
    else {
        eprintln!("REPETITIONS must be an integer from 1 through 10000");
        return ExitCode::FAILURE;
    };
    if repetitions == 0 || repetitions > 10_000 || arguments.next().is_some() {
        eprintln!("REPETITIONS must be an integer from 1 through 10000, with no extra arguments");
        return ExitCode::FAILURE;
    }
    let (data, expected) = if case == "semantic_reports/all" {
        (
            semantic_reports::report_fixture(),
            (0..1_024).map(semantic_reports::expected_report).collect(),
        )
    } else if let Some(name) = case.strip_prefix("semantic_mcdc_proof/") {
        let Some((data, expected)) = semantic_reports::mcdc_case(name) else {
            eprintln!("unknown semantic report case: {case}");
            return ExitCode::FAILURE;
        };
        (data, vec![expected])
    } else {
        eprintln!("unknown semantic report case: {case}");
        return ExitCode::FAILURE;
    };
    if data.report_records(&[], &[]) != expected {
        eprintln!("semantic report parity failed for {case}");
        return ExitCode::FAILURE;
    }
    let mut completed = 0;
    for _ in 0..repetitions {
        let records = black_box(black_box(&data).report_records(&[], &[]));
        completed += black_box(records.len());
    }
    println!("{case}: {repetitions} reports, {completed} records");
    ExitCode::SUCCESS
}
