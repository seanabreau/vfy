use super::matches;

#[test]
fn python_character_classes_and_star_backtracking() {
    // Expected values are from CPython's fnmatch.fnmatchcase.
    for (pattern, value, expected) in [
        ("[^a]", "^", true),
        ("[^a]", "b", false),
        ("[a--b]", "b", true),
        ("[a--b]", "a", false),
        ("[b-a!]", "a", true),
        ("[b-a!a]", "a", false),
        ("[!z-a]", "λ", true),
        ("[]]", "]", true),
        ("[!a", "[!a", true),
        ("[!-a]", "-", false),
        ("[a-b-c]", "-", true),
        ("[a-b-c]", "c", true),
        ("*a*b", "aa\nb", true),
        ("*a*b", "aa\nc", false),
        ("a/**/b", "a/x/y/b", true),
        ("?", "λ", true),
    ] {
        assert_eq!(matches(pattern, value), expected, "{pattern:?} {value:?}");
    }
}

#[test]
fn live_file_patterns_preserve_resolved_discovery_path_policy() {
    use super::super::{Pattern, matches_python_file};
    use std::path::Path;

    for (pattern, path, expected) in [
        ("test_*.py", "/project/suite/test_one.py", true),
        ("*_test.py", "/project/suite/test_one.py", false),
        ("suite/check_?.py", "/project/suite/check_λ.py", true),
        ("suite/check_?.py", "/project/other/check_λ.py", false),
        ("/project/[ab].py", "/project/b.py", true),
        ("[a--b].py", "/project/b.py", true),
        ("[!z-a].py", "/project/λ.py", true),
        ("[b-a!a].py", "/project/b.py", true),
        ("*.py", "/project/test_one.txt", false),
    ] {
        let path = Path::new(path);
        let resolved = Pattern::new(pattern.to_owned()).expect("compiled discovery pattern");
        let compiled = path.extension().is_some_and(|extension| extension == "py")
            && resolved.matches_path(path);
        let live = matches_python_file(path, vec![pattern.to_owned()]);
        assert_eq!(live, expected, "live pattern {pattern:?} path {path:?}");
        assert_eq!(live, compiled, "resolved pattern {pattern:?} path {path:?}");
    }
}
