//! Parse vfy test controls before forwarding the remaining arguments to pytest.
use serde_json::{Map, Value, json};
use std::collections::BTreeSet;

#[derive(Clone, Copy)]
enum Kind {
    Flag,
    Value,
    Append,
    Workers,
    Color,
    Choice(&'static [&'static str]),
}

struct OptionSpec {
    names: &'static [&'static str],
    dest: &'static str,
    kind: Kind,
    help: &'static str,
    metavar: Option<&'static str>,
}

const TEST_OPTIONS: &[OptionSpec] = &[
    OptionSpec {
        names: &["--data-file"],
        dest: "data_file",
        kind: Kind::Value,
        help: "read and write coverage data in this file (default: .coverage, or $COVERAGE_FILE)",
        metavar: Some("PATH"),
    },
    OptionSpec {
        names: &["--rcfile"],
        dest: "rcfile",
        kind: Kind::Value,
        help: "pass this configuration file to pytest as -c and read it for Coverage.py settings; vfy settings come from --config",
        metavar: Some("PATH"),
    },
    OptionSpec {
        names: &["--color"],
        dest: "color",
        kind: Kind::Color,
        help: "",
        metavar: None,
    },
    OptionSpec {
        names: &["--no-color"],
        dest: "color",
        kind: Kind::Flag,
        help: "",
        metavar: None,
    },
    OptionSpec {
        names: &["--engine"],
        dest: "engine",
        kind: Kind::Choice(&["native", "pytest-native", "pytest"]),
        help: "select the test engine (default: native; pytest-native and pytest require installed pytest)",
        metavar: None,
    },
    OptionSpec {
        names: &["--all"],
        dest: "fresh",
        kind: Kind::Flag,
        help: "run every test in scope instead of only tests affected by changes",
        metavar: None,
    },
    OptionSpec {
        names: &["--baseline"],
        dest: "baseline",
        kind: Kind::Value,
        help: "select tests affected since this exported baseline; an incompatible baseline runs all tests",
        metavar: Some("PATH"),
    },
    OptionSpec {
        names: &["--export-baseline"],
        dest: "export_baseline",
        kind: Kind::Value,
        help: "write a portable baseline after a passing --all run",
        metavar: Some("PATH"),
    },
    OptionSpec {
        names: &["--plan-output"],
        dest: "plan_output",
        kind: Kind::Value,
        help: "write the test selection plan to this file",
        metavar: Some("PATH"),
    },
    OptionSpec {
        names: &["--plan-only"],
        dest: "plan_only",
        kind: Kind::Flag,
        help: "write the test selection plan without running tests",
        metavar: None,
    },
    OptionSpec {
        names: &["--audit-selection"],
        dest: "audit_selection",
        kind: Kind::Flag,
        help: "run all tests and check that the selection skipped no failing test",
        metavar: None,
    },
    OptionSpec {
        names: &["--confirm-failures"],
        dest: "confirm_failures",
        kind: Kind::Flag,
        help: "rerun failed tests in a fresh serial process",
        metavar: None,
    },
    OptionSpec {
        names: &["--history"],
        dest: "history",
        kind: Kind::Value,
        help: "use this test history file for recorded durations and outcomes (default: configured history, else .vfy/test-history.sqlite3)",
        metavar: Some("PATH"),
    },
    OptionSpec {
        names: &["--no-replay"],
        dest: "no_replay",
        kind: Kind::Flag,
        help: "run without reusing saved coverage from the passing baseline",
        metavar: None,
    },
    OptionSpec {
        names: &["--watch"],
        dest: "watch",
        kind: Kind::Flag,
        help: "rerun tests after source changes",
        metavar: None,
    },
    OptionSpec {
        names: &["-j", "-n", "--workers"],
        dest: "workers",
        kind: Kind::Workers,
        help: "set the number of test workers (default: 1)",
        metavar: Some("N|auto|logical"),
    },
    OptionSpec {
        names: &["--native-scheduling"],
        dest: "native_scheduling",
        kind: Kind::Choice(&["file", "scope", "cases", "striped", "demand"]),
        help: "assign tests to workers by file, scope, case range, interleaved case, or demand batch (default: demand with multiple native workers, file with pytest-native)",
        metavar: None,
    },
    OptionSpec {
        names: &["--source"],
        dest: "source",
        kind: Kind::Append,
        help: "analyze this source root to select affected tests; repeatable",
        metavar: Some("PATH"),
    },
];

fn defaults() -> Map<String, Value> {
    json!({"config_file":null,"isolated":false,"color":null,"verbose":0,"quiet":false,"command":"test","data_file":null,"rcfile":null,"engine":"native","fresh":false,"baseline":null,"export_baseline":null,"plan_output":null,"plan_only":false,"audit_selection":false,"confirm_failures":false,"history":null,"no_replay":false,"watch":false,"workers":null,"native_scheduling":null,"source":null,"target":[],"pytest_args":[],"mode":"full"}).as_object().cloned().unwrap_or_default()
}

#[must_use]
pub fn test_cli_schema() -> Vec<Value> {
    let specs: Vec<Value> = TEST_OPTIONS.iter().map(|spec| {
        let (kind, choices) = match spec.kind {
            Kind::Flag => ("flag", Value::Null), Kind::Value => ("value", Value::Null),
            Kind::Append => ("append", Value::Null), Kind::Workers => ("workers", Value::Null),
            Kind::Color => ("color", json!(["auto", "always", "never", "yes", "no"])),
            Kind::Choice(values) => ("choice", json!(values)),
        };
        json!({"names": spec.names, "dest": spec.dest, "kind":kind, "choices":choices, "help":spec.help, "metavar":spec.metavar})
    }).collect();
    specs
}

fn command_index(args: &[String]) -> Option<usize> {
    let mut index = 0;
    while index < args.len() {
        let token = args[index].as_str();
        if token == "--config"
            || token == "--color"
                && args
                    .get(index + 1)
                    .is_some_and(|v| ["auto", "always", "never"].contains(&v.as_str()))
        {
            index += 2;
        } else if token.starts_with('-') {
            index += 1;
        } else {
            return Some(index);
        }
    }
    None
}

#[must_use]
pub fn is_test_cli(args: &[String]) -> bool {
    command_index(args).is_some_and(|index| args[index] == "test")
}

/// Parse test-command options and retain forwarded pytest arguments.
///
/// The token set contains the negative-looking arguments that the active
/// interpreter's argparse matcher recognizes as positional numbers.
///
/// # Errors
///
/// Returns a diagnostic when an option or its value is invalid.
pub fn parse_test_cli(
    args: &[String],
    negative_positional_tokens: &BTreeSet<String>,
) -> Result<Value, String> {
    let command = command_index(args).ok_or("missing test command")?;
    if args[command] != "test" {
        return Err("not a test command".to_owned());
    }
    if args[command + 1..].iter().any(|v| v == "--") {
        return Err(
            "vfy test accepts pytest options directly; remove the `--` separator".to_owned(),
        );
    }
    let mut values = defaults();
    parse_global_options(&args[..command], &mut values)?;
    let mut index = command + 1;
    let mut targets = Vec::new();
    let mut forwarded = Vec::new();
    let mut in_tail = false;
    let mut first_positional_seen = false;
    while index < args.len() {
        let token = args[index].as_str();
        let (name, mut attached) = token
            .split_once('=')
            .map_or((token, None), |(a, b)| (a, Some(b)));
        let mut found = TEST_OPTIONS.iter().find(|spec| spec.names.contains(&name));
        if found.is_none()
            && token.starts_with('-')
            && !token.starts_with("--")
            && token.len() > 2
            && let Some(prefix @ ("-j" | "-n")) = token.get(..2)
        {
            found = TEST_OPTIONS
                .iter()
                .find(|spec| spec.names.contains(&prefix));
            attached = token.get(2..);
        }
        if found.is_none() && !first_positional_seen && name.starts_with("--") {
            let matches: Vec<_> = TEST_OPTIONS
                .iter()
                .filter(|spec| spec.names.iter().any(|option| option.starts_with(name)))
                .collect();
            if matches.len() > 1 {
                return Err(format!("ambiguous option: {name}"));
            }
            if matches.len() == 1 && !in_tail {
                found = matches.first().copied();
            }
        }
        let Some(spec) = found else {
            if is_option_like(token, negative_positional_tokens) {
                in_tail = true;
            } else {
                first_positional_seen = true;
            }
            if in_tail {
                forwarded.push(token.to_owned());
            } else {
                targets.push(token.to_owned());
            }
            index += 1;
            continue;
        };
        if !targets.is_empty() {
            in_tail = true;
        }
        apply_test_option(
            spec,
            name,
            attached,
            args,
            &mut index,
            &mut values,
            negative_positional_tokens,
        )?;
        index += 1;
    }
    if values.get("engine") != Some(&json!("pytest"))
        && let Some(rewritten) = rewrite_serial_xdist(args)
    {
        return parse_test_cli(&rewritten, negative_positional_tokens);
    }
    if let Some(color) = values.get("color").and_then(Value::as_str) {
        let color = match color {
            "always" => "yes",
            "never" => "no",
            other => other,
        };
        forwarded.insert(0, format!("--color={color}"));
    } else {
        values.insert("color".to_owned(), json!("auto"));
    }
    values.insert("target".to_owned(), json!(targets));
    values.insert("pytest_args".to_owned(), json!(forwarded));
    Ok(Value::Object(values))
}

// argparse treats a leading minus as positional only for decimal integers and
// decimal fractions; exponent notation still starts an option.
fn is_option_like(value: &str, negative_positional_tokens: &BTreeSet<String>) -> bool {
    if !value.starts_with('-') || value == "-" {
        return false;
    }
    let name = value.split_once('=').map_or(value, |(name, _)| name);
    let registered = TEST_OPTIONS.iter().any(|spec| spec.names.contains(&name))
        || matches!(name, "-h" | "--help")
        || (value.len() > 2
            && (value.starts_with("-j") || value.starts_with("-n") || value.starts_with("-h")));
    registered || (!value.contains(' ') && !negative_positional_tokens.contains(value))
}

fn parse_global_options(args: &[String], values: &mut Map<String, Value>) -> Result<(), String> {
    let mut index = 0;
    while index < args.len() {
        let token = args[index].as_str();
        let (name, attached) = token
            .split_once('=')
            .map_or((token, None), |(a, b)| (a, Some(b)));
        let name = if name.starts_with("--col") && "--color".starts_with(name) {
            "--color"
        } else {
            name
        };
        match name {
            "--config" => {
                let value = if let Some(v) = attached {
                    v
                } else {
                    index += 1;
                    args.get(index)
                        .ok_or("argument --config: expected one argument")?
                };
                values.insert("config_file".to_owned(), json!(value));
            }
            "--isolated" => {
                values.insert("isolated".to_owned(), json!(true));
            }
            "--no-color" => {
                values.insert("color".to_owned(), json!("never"));
            }
            "--color" => {
                let value = if let Some(v) = attached {
                    v
                } else if args
                    .get(index + 1)
                    .is_some_and(|v| ["auto", "always", "never"].contains(&v.as_str()))
                {
                    index += 1;
                    args[index].as_str()
                } else {
                    "always"
                };
                if !["auto", "always", "never"].contains(&value) {
                    return Err(format!("argument --color: invalid choice: {value:?}"));
                }
                values.insert("color".to_owned(), json!(value));
            }
            "-q" | "--quiet" => {
                values.insert("quiet".to_owned(), json!(true));
            }
            "-v" | "--verbose" => {
                let count = values.get("verbose").and_then(Value::as_u64).unwrap_or(0);
                values.insert("verbose".to_owned(), json!(count + 1));
            }
            _ if token.starts_with('-')
                && token.len() > 1
                && token[1..].chars().all(|c| c == 'v') =>
            {
                let count = values.get("verbose").and_then(Value::as_u64).unwrap_or(0);
                values.insert(
                    "verbose".to_owned(),
                    json!(count.saturating_add(u64::try_from(token.len() - 1).unwrap_or(u64::MAX))),
                );
            }
            _ => return Err(format!("unrecognized arguments: {token}")),
        }
        index += 1;
    }
    if values.get("quiet") == Some(&json!(true))
        && values.get("verbose").and_then(Value::as_u64).unwrap_or(0) != 0
    {
        return Err("argument -q/--quiet: not allowed with argument -v/--verbose".to_owned());
    }
    Ok(())
}

fn apply_test_option(
    spec: &OptionSpec,
    name: &str,
    attached: Option<&str>,
    args: &[String],
    index: &mut usize,
    values: &mut Map<String, Value>,
    negative_positional_tokens: &BTreeSet<String>,
) -> Result<(), String> {
    match spec.kind {
        Kind::Flag => {
            if attached.is_some() {
                return Err(format!("argument {name}: ignored explicit argument"));
            }
            values.insert(
                spec.dest.to_owned(),
                if spec.names == ["--no-color"] {
                    json!("never")
                } else {
                    json!(true)
                },
            );
        }
        kind => {
            let value = if let Some(value) = attached {
                value
            } else if matches!(kind, Kind::Color)
                && args
                    .get(*index + 1)
                    .is_none_or(|v| is_option_like(v, negative_positional_tokens))
            {
                "always"
            } else {
                *index += 1;
                let next = args
                    .get(*index)
                    .ok_or_else(|| format!("argument {name}: expected one argument"))?;
                if is_option_like(next, negative_positional_tokens) {
                    return Err(format!("argument {name}: expected one argument"));
                }
                next
            };
            let parsed = match kind {
                Kind::Color => {
                    if !["auto", "always", "never", "yes", "no"].contains(&value) {
                        return Err(format!("argument {name}: invalid choice: {value:?}"));
                    }
                    json!(value)
                }
                Kind::Choice(choices) => {
                    if !choices.contains(&value) {
                        return Err(format!("argument {name}: invalid choice: {value:?}"));
                    }
                    json!(value)
                }
                Kind::Workers => {
                    if value == "0" {
                        json!(1)
                    } else if ["auto", "logical"].contains(&value) {
                        json!(value)
                    } else {
                        let n = value
                            .parse::<u32>()
                            .ok()
                            .filter(|n| (1..=crate::MAX_WORKERS).contains(n))
                            .ok_or(crate::config::WORKERS_RANGE)?;
                        json!(n)
                    }
                }
                _ => json!(value),
            };
            if matches!(kind, Kind::Append) {
                let entry = values.entry(spec.dest.to_owned()).or_insert(Value::Null);
                if entry.is_null() {
                    *entry = json!([]);
                }
                if let Some(list) = entry.as_array_mut() {
                    list.push(parsed);
                }
            } else {
                values.insert(spec.dest.to_owned(), parsed);
            }
        }
    }
    Ok(())
}

/// Preserve pytest-xdist's serial override alongside vfy's one-worker setting.
#[must_use]
pub fn rewrite_serial_xdist(args: &[String]) -> Option<Vec<String>> {
    let mut rewritten = Vec::with_capacity(args.len());
    let mut changed = false;
    let mut position = 0;
    while position < args.len() {
        if matches!(args[position].as_str(), "-n0" | "-n=0")
            || (args[position] == "-n" && args.get(position + 1).is_some_and(|v| v == "0"))
        {
            if args[position] == "-n" {
                position += 1;
            }
            rewritten.extend(["--workers=1".to_owned(), "--numprocesses=0".to_owned()]);
            changed = true;
        } else {
            rewritten.push(args[position].clone());
        }
        position += 1;
    }
    changed.then_some(rewritten)
}

#[cfg(test)]
mod tests;
