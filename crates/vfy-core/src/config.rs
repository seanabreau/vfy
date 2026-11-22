//! CLI and pytest option precedence over normalized project configuration.

use serde_json::{Map, Value, json};

#[cfg(test)]
mod tests;

fn present(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(value) => *value,
        Value::String(value) => !value.is_empty(),
        Value::Array(value) => !value.is_empty(),
        Value::Object(value) => !value.is_empty(),
        Value::Number(value) => value.as_f64() != Some(0.0),
    }
}

fn fallback(values: &[&Value]) -> Value {
    values
        .iter()
        .find(|value| present(value))
        .or_else(|| values.last())
        .map_or(Value::Null, |value| (*value).clone())
}

fn unset(args: &mut Value, name: &str, value: Value) {
    if args.get(name).is_some_and(Value::is_null) {
        args[name] = value;
    }
}

fn defaulted(config: &Value, name: &str, default: Value) -> Value {
    config.get(name).cloned().unwrap_or(default)
}

fn resolve_run(args: &mut Value, config: &Value) {
    let run = &config["run"];
    for (name, key, default) in [
        ("branch", "branch", json!(false)),
        ("parallel_mode", "parallel", json!(false)),
    ] {
        unset(args, name, defaulted(run, key, default));
    }
    unset(
        args,
        "decisions",
        defaulted(&config["vfy"]["run"], "decisions", json!(false)),
    );
    for name in ["source", "include", "omit", "context"] {
        unset(args, name, run[name].clone());
    }
}

fn history(args: &mut Value, settings: &Value) {
    if args["history"].is_null() {
        args["history"] = defaulted(settings, "history", json!(".vfy/test-history.sqlite3"));
    }
}

/// The accepted values for a worker count, shared by the CLI and configuration.
pub(crate) const WORKERS_RANGE: &str = "workers must be auto, logical, or an integer from 1 to 256";

fn workers(args: &mut Value, settings: &Value, default: Value) -> Result<(), String> {
    if !args["workers"].is_null() {
        return Ok(());
    }
    let configured = defaulted(settings, "workers", default);
    let value = &configured;
    args["workers"] = match value.as_str() {
        Some("auto" | "logical") | None => value.clone(),
        Some(value) => json!(
            value
                .parse::<u32>()
                .ok()
                .filter(|value| (1..=crate::MAX_WORKERS).contains(value))
                .ok_or_else(|| WORKERS_RANGE.to_owned())?
        ),
    };
    Ok(())
}

fn resolve_tests(args: &mut Value, config: &Value, command: &str) -> Result<(), String> {
    let settings = &config["vfy"]["test"];
    history(args, settings);
    let source = if command == "test" {
        if args["engine"].as_str() == Some("native") {
            args["_independent_workers_configured"] =
                json!(!args["workers"].is_null() || !settings["workers"].is_null());
        }
        args["mode"] = if present(&args["fresh"]) {
            json!("safe")
        } else {
            fallback(&[&settings["selection"], &json!("safe")])
        };
        let default_workers = if matches!(args["engine"].as_str(), Some("pytest-native" | "native"))
        {
            json!(1)
        } else {
            json!("auto")
        };
        workers(args, settings, default_workers)?;
        fallback(&[&settings["source"], &config["run"]["source"], &json!([])])
    } else {
        fallback(&[&config["run"]["source"], &json!([])])
    };
    unset(
        args,
        "source",
        if source.is_null() { json!([]) } else { source },
    );
    Ok(())
}

fn resolve_mutation(args: &mut Value, config: &Value) -> Result<(), String> {
    let settings = &config["vfy"]["mutate"];
    history(args, settings);
    if args["timeout"].is_null() {
        args["timeout"] = defaulted(settings, "timeout", json!(30.0));
    }
    unset(
        args,
        "timeout_policy",
        defaulted(settings, "timeout_policy", json!("adaptive")),
    );
    let default_workers = if args["engine"].as_str() == Some("native") {
        json!(1)
    } else {
        json!("auto")
    };
    workers(args, settings, default_workers)?;
    let source = fallback(&[&settings["source"], &config["run"]["source"]]);
    unset(
        args,
        "source",
        if source.is_null() { json!([]) } else { source },
    );
    Ok(())
}

fn resolve_check(args: &mut Value, config: &Value) {
    let settings = &config["vfy"]["check"];
    unset(args, "history", json!(".vfy/test-history.sqlite3"));
    for name in ["include", "omit", "contexts", "exclude_decisions", "output"] {
        args[name] = fallback(&[&args[name], &settings[name]]);
    }
    args["source"] = fallback(&[&args["source"], &config["run"]["source"]]);
    args["format"] = fallback(&[&args["format"], &settings["format"], &json!("text")]);
    for name in [
        "fail_under_decisions",
        "fail_under_conditions",
        "fail_under_mcdc",
    ] {
        unset(args, name, settings[name].clone());
    }
}

fn resolve_pytest(args: &mut Value, config: &Value) {
    args["cov_decisions"] = fallback(&[
        &args["cov_decisions"],
        &defaulted(&config["vfy"]["run"], "decisions", json!(false)),
    ]);
    for name in ["decisions", "conditions", "mcdc"] {
        unset(
            args,
            &format!("cov_fail_under_{name}"),
            config["vfy"]["check"][format!("fail_under_{name}")].clone(),
        );
    }
    unset(
        args,
        "cov_precision",
        defaulted(&config["report"], "precision", json!(0)),
    );
    unset(
        args,
        "cov_fail_under",
        config["report"]["fail_under"].clone(),
    );
}

fn resolve_reports(args: &mut Value, config: &Value, command: &str) {
    let report = &config["report"];
    args["relative_files"] = defaulted(&config["run"], "relative_files", json!(false));
    unset(args, "include", report["include"].clone());
    // `[run] omit` limits measurement only; Coverage.py reports honor `[report] omit`.
    unset(args, "omit", report["omit"].clone());
    unset(args, "contexts", report["contexts"].clone());
    unset(args, "precision", defaulted(report, "precision", json!(0)));
    unset(args, "fail_under", report["fail_under"].clone());
    unset(
        args,
        "ignore_errors",
        defaulted(report, "ignore_errors", json!(false)),
    );
    match command {
        "report" => {
            for name in ["show_missing", "skip_covered", "skip_empty"] {
                unset(args, name, defaulted(report, name, json!(false)));
            }
            unset(args, "sort", report["sort"].clone());
            unset(args, "format", defaulted(report, "format", json!("text")));
        }
        "xml" | "json" | "lcov" => {
            let destination = format!("coverage.{command}");
            args["output"] = fallback(&[
                &args["output"],
                &defaulted(&config[command], "output", json!(destination)),
            ]);
            if command == "xml" {
                args["xml_package_depth"] = defaulted(&config["xml"], "package_depth", json!(99));
            }
            if command == "json" {
                for name in ["pretty_print", "show_contexts"] {
                    unset(args, name, defaulted(&config[command], name, json!(false)));
                }
            }
        }
        "html" => {
            args["directory"] = fallback(&[
                &args["directory"],
                &defaulted(&config["html"], "directory", json!("htmlcov")),
            ]);
            args["title"] = fallback(&[
                &args["title"],
                &defaulted(&config["html"], "title", json!("Coverage report")),
            ]);
        }
        "annotate" => {
            args["directory"] = fallback(&[&args["directory"], &config["annotate"]["directory"]]);
        }
        _ => {}
    }
}

/// Resolve explicit command arguments against normalized configuration and process input.
///
/// # Errors
/// Rejects arguments without a command or invalid configured worker counts.
pub fn resolve_cli_options(
    arguments: Map<String, Value>,
    config: &Value,
    coverage_file: Option<&str>,
) -> Result<Map<String, Value>, String> {
    let mut args = Value::Object(arguments);
    let command = args["command"]
        .as_str()
        .ok_or_else(|| "missing CLI command".to_owned())?
        .to_owned();
    let environment = if present(&args["isolated"]) {
        Value::Null
    } else {
        json!(coverage_file)
    };
    let data_file = fallback(&[
        &environment,
        &defaulted(&config["run"], "data_file", json!(".coverage")),
    ]);
    unset(&mut args, "data_file", data_file);
    if matches!(command.as_str(), "test" | "explain" | "mutate" | "check")
        && !present(&args["data_file"])
    {
        args["data_file"] = fallback(&[
            &environment,
            &defaulted(&config["run"], "data_file", json!(".coverage")),
        ]);
    }
    match command.as_str() {
        "run" => resolve_run(&mut args, config),
        "test" | "explain" => resolve_tests(&mut args, config, &command)?,
        "mutate" => resolve_mutation(&mut args, config)?,
        "check" => resolve_check(&mut args, config),
        "pytest" => resolve_pytest(&mut args, config),
        "report" | "xml" | "json" | "lcov" | "html" | "annotate" => {
            resolve_reports(&mut args, config, &command);
        }
        _ => {}
    }
    match args {
        Value::Object(args) => Ok(args),
        _ => Err("CLI arguments must be an object".to_owned()),
    }
}
