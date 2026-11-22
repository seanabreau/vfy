//! Pytest's plugin preparse recognizes literal `-p` tokens before argparse.

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) enum PluginDirective<'a> {
    Enable(&'a str),
    Disable(&'a str),
}

impl<'a> PluginDirective<'a> {
    pub(super) fn parse(argument: &'a str) -> Self {
        argument
            .strip_prefix("no:")
            .map_or(Self::Enable(argument), Self::Disable)
    }
}

fn trim_operand(argument: &str) -> &str {
    argument.trim_matches(|character: char| {
        character.is_whitespace() || matches!(character, '\u{1c}'..='\u{1f}')
    })
}

pub(super) fn plugin_arguments(arguments: &[String]) -> impl Iterator<Item = &str> {
    let mut arguments = arguments.iter();
    std::iter::from_fn(move || {
        while let Some(argument) = arguments.next() {
            if argument == "-p" {
                return arguments.next().map(|value| trim_operand(value));
            }
            if let Some(value) = argument.strip_prefix("-p") {
                return Some(trim_operand(value));
            }
        }
        None
    })
}

pub(super) fn raw_provider_excluded(arguments: &[String], provider: &str) -> bool {
    arguments.iter().any(|argument| matches!(PluginDirective::parse(argument), PluginDirective::Disable(name) if name == provider))
}

pub(super) fn validate_exclusion(name: &str) -> Result<(), String> {
    if matches!(name, "mark" | "main" | "runner" | "fixtures" | "helpconfig") {
        return Err(format!("plugin {name} cannot be disabled"));
    }
    if name.ends_with("conftest.py") {
        return Err(format!(
            "Blocking conftest files using -p is not supported: -p no:{name}\n\
             conftest.py files are not plugins and cannot be disabled via -p.\n"
        ));
    }
    Ok(())
}

#[cfg(test)]
mod tests;
