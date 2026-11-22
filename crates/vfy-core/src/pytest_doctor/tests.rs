use super::{ConftestFinding, ConftestFindingKind, native_plugin_module_supported, scan_conftests};
use std::fs;

#[test]
fn outer_hookimpl_declaration_replaces_inner_options() {
    let project = tempfile::tempdir().expect("create project");
    fs::write(
        project.path().join("conftest.py"),
        concat!(
            "@pytest.hookimpl(wrapper=False)\n",
            "@hookimpl(wrapper=True)\n",
            "def pytest_runtest_setup(item): pass\n",
            "@hookimpl\n",
            "@pytest.hookimpl(specname='pytest_runtest_call', hookwrapper=True)\n",
            "def pytest_runtest_teardown(item): pass\n",
            "@pytest.hookimpl(wrapper=choose_wrapper())\n",
            "@hookimpl(wrapper=True)\n",
            "def pytest_runtest_setup(item): yield\n",
            "@hookimpl(**options)\n",
            "@pytest.hookimpl(specname='pytest_runtest_call', hookwrapper=True)\n",
            "def pytest_runtest_teardown(item): yield\n",
            "@pytest.hookimpl(specname='', wrapper=True)\n",
            "def pytest_runtest_setup(item): yield\n",
        ),
    )
    .expect("write conftest");

    let scan = scan_conftests(project.path());
    let actual = scan
        .findings
        .iter()
        .map(|finding| (finding.kind.as_str(), finding.name.as_str(), finding.line))
        .collect::<Vec<_>>();
    assert_eq!(
        actual,
        [("unsupported_hook_wrapper", "pytest_runtest_setup", Some(14))]
    );
}

#[test]
fn reports_only_literal_phase_wrappers_including_specname_aliases() {
    let project = tempfile::tempdir().expect("create project");
    fs::write(
        project.path().join("conftest.py"),
        concat!(
            "@pytest.hookimpl(wrapper=True)\n",
            "def pytest_runtest_setup(item): yield\n",
            "@hookimpl(hookwrapper=True, specname='pytest_runtest_teardown')\n",
            "def phase_alias(item): yield\n",
            "@pytest.hookimpl(specname='pytest_runtest_setup', wrapper=True)\n",
            "def pytest_phase_alias(item): yield\n",
            "def pytest_runtest_teardown(item): pass\n",
            "@pytest.hookimpl(wrapper=False, hookwrapper=False)\n",
            "def pytest_runtest_setup(item): pass\n",
            "@pytest.hookimpl(wrapper=enable_wrapper())\n",
            "def pytest_runtest_setup(item): yield\n",
            "@pytest.hookimpl(wrapper=True)\n",
            "def pytest_collection_modifyitems(items): yield\n",
            "@other.hookimpl(wrapper=True)\n",
            "def pytest_runtest_setup(item): yield\n",
            "@pytest.hookimpl(specname=choose_hook(), wrapper=True)\n",
            "def pytest_runtest_setup(item): yield\n",
            "@pytest.hookimpl(wrapper=True, **options)\n",
            "def pytest_runtest_setup(item): yield\n",
            "pytest_plugins = discover_plugins()\n",
        ),
    )
    .expect("write conftest");

    let scan = scan_conftests(project.path());
    let actual = scan
        .findings
        .iter()
        .map(|finding| (finding.kind.as_str(), finding.name.as_str(), finding.line))
        .collect::<Vec<_>>();
    assert_eq!(
        actual,
        [
            ("unsupported_hook_wrapper", "pytest_runtest_setup", Some(2)),
            (
                "unsupported_hook_wrapper",
                "pytest_runtest_teardown",
                Some(4)
            ),
            ("unsupported_hook_wrapper", "pytest_runtest_setup", Some(6)),
            ("dynamic_plugin", "pytest_plugins", Some(20)),
        ]
    );
    assert_eq!(scan.conftest_count, 1);
    assert!(!scan.truncated);
}

#[test]
fn scans_top_level_plugin_forms_and_hook_specnames_in_source_order() {
    let project = tempfile::tempdir().expect("create project");
    let path = project.path().join("conftest.py");
    fs::write(
        &path,
        concat!(
            "pytest_plugins = ('plugin.one', 'plugin.two')\n",
            "other = pytest_plugins = ['plugin.three']\n",
            "pytest_plugins: object = load_plugins()\n",
            "pytest_plugins += 'plugin.four'\n",
            "@pytest.hookimpl(specname='pytest_unknown')\n",
            "def pytest_configure(config): pass\n",
            "async def pytest_surprise(): pass\n",
            "def pytest_report_header(config): pass\n",
            "pytest_plugins = 'pytester,pytester_assertions'\n",
        ),
    )
    .expect("write conftest");

    let scan = scan_conftests(project.path());
    let actual = scan
        .findings
        .iter()
        .map(|finding| (finding.kind.as_str(), finding.name.as_str(), finding.line))
        .collect::<Vec<_>>();
    assert_eq!(
        actual,
        [
            ("plugin", "plugin.one", Some(1)),
            ("plugin", "plugin.two", Some(1)),
            ("plugin", "plugin.three", Some(2)),
            ("dynamic_plugin", "pytest_plugins", Some(3)),
            ("plugin", "plugin.four", Some(4)),
            ("unsupported_hook", "pytest_unknown", Some(6)),
            ("unsupported_hook", "pytest_surprise", Some(7)),
            ("plugin", "pytester", Some(9)),
            ("plugin", "pytester_assertions", Some(9)),
        ]
    );
    assert_eq!(scan.conftest_count, 1);
    assert!(!scan.truncated);
    assert!(scan.findings.iter().all(|finding| finding.path == path));
}

#[test]
fn skips_ignored_directories_and_orders_findings_by_path() {
    let project = tempfile::tempdir().expect("create project");
    for directory in ["z", "a", ".venv", "vendor", ".hidden"] {
        fs::create_dir(project.path().join(directory)).expect("create child");
        fs::write(
            project.path().join(directory).join("conftest.py"),
            "pytest_plugins = 'plug'\n",
        )
        .expect("write conftest");
    }

    let scan = scan_conftests(project.path());
    let paths = scan
        .findings
        .iter()
        .map(|finding| {
            finding
                .path
                .parent()
                .unwrap()
                .file_name()
                .unwrap()
                .to_string_lossy()
                .into_owned()
        })
        .collect::<Vec<_>>();
    assert_eq!(paths, ["a", "z"]);
    assert_eq!(scan.conftest_count, 2);
}

#[test]
fn reports_syntax_and_read_failures_as_findings() {
    let project = tempfile::tempdir().expect("create project");
    let path = project.path().join("conftest.py");
    fs::write(&path, "def pytest_configure(:\n    pass\n").expect("write invalid syntax");
    let scan = scan_conftests(project.path());
    let ConftestFinding {
        kind, name, line, ..
    } = &scan.findings[0];
    assert_eq!(*kind, ConftestFindingKind::SyntaxError);
    assert!(name.starts_with("cannot inspect conftest:"));
    assert_eq!(*line, Some(1));

    fs::write(&path, [0xff]).expect("write invalid UTF-8");
    let scan = scan_conftests(project.path());
    assert_eq!(scan.findings[0].kind, ConftestFindingKind::SyntaxError);
    assert_eq!(scan.findings[0].line, None);
}

#[test]
fn stops_after_one_thousand_conftests_in_sorted_walk_order() {
    let project = tempfile::tempdir().expect("create project");
    for index in 0..1_001 {
        let directory = project.path().join(format!("{index:04}"));
        fs::create_dir(&directory).expect("create child");
        fs::write(directory.join("conftest.py"), "pytest_plugins = 'plug'\n")
            .expect("write conftest");
    }

    let scan = scan_conftests(project.path());

    assert_eq!(scan.conftest_count, 1_000);
    assert!(scan.truncated);
    assert_eq!(scan.findings.len(), 1_000);
    assert!(scan.findings[0].path.ends_with("0000/conftest.py"));
    assert!(scan.findings[999].path.ends_with("0999/conftest.py"));
}

#[test]
fn empty_literals_do_not_request_plugins_and_unresolved_values_are_reported() {
    let project = tempfile::tempdir().expect("create project");
    fs::write(
        project.path().join("conftest.py"),
        "pytest_plugins = 0\npytest_plugins = {}\npytest_plugins = set()\npytest_plugins = ['plug', find_plugin()]\n",
    )
    .expect("write conftest");

    let scan = scan_conftests(project.path());

    assert_eq!(scan.findings.len(), 1);
    assert_eq!(scan.findings[0].kind, ConftestFindingKind::DynamicPlugin);
    assert_eq!(scan.findings[0].line, Some(4));
}

#[cfg(unix)]
#[test]
fn does_not_follow_symlink_directories_or_count_a_directory_named_conftest() {
    use std::os::unix::fs::symlink;

    let project = tempfile::tempdir().expect("create project");
    let outside = tempfile::tempdir().expect("create outside project");
    fs::write(
        outside.path().join("conftest.py"),
        "pytest_plugins = 'outside'\n",
    )
    .expect("write outside conftest");
    symlink(outside.path(), project.path().join("linked")).expect("link outside directory");
    fs::create_dir(project.path().join("conftest.py")).expect("create same-named directory");

    let scan = scan_conftests(project.path());

    assert_eq!(scan.conftest_count, 0);
    assert!(scan.findings.is_empty());

    fs::remove_dir(project.path().join("conftest.py")).expect("remove same-named directory");
    symlink(outside.path(), project.path().join("conftest.py"))
        .expect("link directory as conftest");
    let scan = scan_conftests(project.path());
    assert_eq!(scan.conftest_count, 0);

    fs::remove_file(project.path().join("conftest.py")).expect("remove directory link");
    symlink(
        project.path().join("missing.py"),
        project.path().join("conftest.py"),
    )
    .expect("create broken file link");
    let scan = scan_conftests(project.path());
    assert_eq!(scan.conftest_count, 1);
    assert_eq!(scan.findings[0].kind, ConftestFindingKind::SyntaxError);
}

#[test]
fn plugin_modules_are_admitted_only_when_every_hook_is_visible_and_supported() {
    let directory = tempfile::tempdir().expect("plugin fixture directory");
    for (source, admitted) in [
        (
            "import pytest\n@pytest.fixture\ndef httpbin():\n    return 1\n",
            true,
        ),
        (
            "def pytest_addoption(parser):\n    pass\n\
             @pytest.hookimpl(wrapper=True)\ndef pytest_collection_modifyitems(items):\n    yield\n",
            true,
        ),
        (
            "try:\n    import x\nexcept ImportError:\n    def pytest_configure(config):\n        pass\n",
            true,
        ),
        (
            "@pytest.hookimpl(specname='pytest_pyfunc_call')\ndef helper():\n    pass\n",
            true,
        ),
        ("def pytest_pyfunc_call(pyfuncitem):\n    pass\n", false),
        ("def pytest_runtest_setup(item):\n    pass\n", false),
        (
            "@pytest.hookimpl(specname='pytest_runtest_call')\ndef pytest_configure():\n    pass\n",
            false,
        ),
        (
            "if True:\n    def pytest_runtest_call(item):\n        pass\n",
            false,
        ),
        (
            "for _ in (0,):\n    def pytest_runtest_call(item):\n        pass\n",
            false,
        ),
        (
            "while enabled:\n    def pytest_runtest_call(item):\n        pass\n",
            false,
        ),
        (
            "with context():\n    def pytest_runtest_call(item):\n        pass\n",
            false,
        ),
        (
            "match value:\n    case _:\n        def pytest_runtest_call(item):\n            pass\n",
            false,
        ),
        ("for pytest_runtest_call in hooks:\n    pass\n", false),
        ("with context() as pytest_runtest_call:\n    pass\n", false),
        (
            "match value:\n    case pytest_runtest_call:\n        pass\n",
            false,
        ),
        (
            "match value:\n    case [*pytest_hooks]:\n        pass\n",
            false,
        ),
        (
            "match value:\n    case {**pytest_hooks}:\n        pass\n",
            false,
        ),
        ("(pytest_runtest_call := callback)\n", false),
        ("class pytest_runtest_call:\n    pass\n", false),
        (
            "class Helper:\n    def pytest_runtest_call(self):\n        pass\n",
            false,
        ),
        (
            "@pytest.hookimpl(specname=target)\ndef pytest_configure(item):\n    pass\n",
            false,
        ),
        (
            "@pytest.hookimpl(**options)\ndef pytest_configure(item):\n    pass\n",
            false,
        ),
        (
            "@impl(specname='pytest_runtest_call')\ndef pytest_configure(item):\n    pass\n",
            false,
        ),
        (
            "@pytest.hookimpl(specname=None)\ndef pytest_configure(config):\n    pass\n",
            true,
        ),
        (
            "for _ in (0,):\n    def pytest_configure(config):\n        pass\n",
            true,
        ),
        ("from ._hooks import pytest_runtest_call\n", false),
        ("from ._hooks import *\n", false),
        ("import hooks as pytest_hooks\n", false),
        ("pytest_runtest_call = staticmethod(print)\n", false),
        ("pytest_plugins = ['other']\n", false),
        ("def broken(:\n", false),
    ] {
        let path = directory.path().join("plugin.py");
        std::fs::write(&path, source).expect("write plugin module");
        assert_eq!(native_plugin_module_supported(&path), admitted, "{source}");
    }
    assert!(!native_plugin_module_supported(
        &directory.path().join("missing.py")
    ));
}

#[test]
fn plugin_class_hook_methods_must_be_supported() {
    let directory = tempfile::tempdir().expect("temporary plug-in directory");
    for (source, admitted) in [
        (
            "class Registered:\n    def pytest_report_collectionfinish(self, items):\n        pass\n",
            false,
        ),
        (
            "class Outer:\n    class Inner:\n        def pytest_runtest_call(self, item):\n            pass\n",
            false,
        ),
        (
            "class Registered:\n    def pytest_collection_modifyitems(self, items):\n        pass\n",
            true,
        ),
        ("class State:\n    def record(self):\n        pass\n", true),
    ] {
        let path = directory.path().join("plugin.py");
        std::fs::write(&path, source).expect("write plugin module");
        assert_eq!(native_plugin_module_supported(&path), admitted, "{source}");
    }
}

#[test]
fn same_package_hook_reexports_require_supported_resolvable_sources() {
    let directory = tempfile::tempdir().expect("temporary plug-in directory");
    let package = directory.path().join("plugin");
    std::fs::create_dir(&package).expect("create plug-in package");
    let entry = package.join("__init__.py");
    let hooks = package.join("options.py");
    for (declaration, source, admitted) in [
        (
            "from plugin.options import pytest_configure\n",
            "def pytest_configure(config):\n    pass\n",
            true,
        ),
        (
            "from .options import pytest_configure\n",
            "def pytest_configure(config):\n    pass\n",
            true,
        ),
        (
            "from .options import pytest_runtest_call\n",
            "def pytest_runtest_call(item):\n    pass\n",
            false,
        ),
        (
            "from .options import pytest_runtest_call as pytest_configure\n",
            "def pytest_runtest_call(item):\n    pass\n",
            false,
        ),
        (
            "from .options import pytest_configure\n",
            "from . import pytest_configure\n",
            false,
        ),
        (
            "from ..options import pytest_configure\n",
            "def pytest_configure(config):\n    pass\n",
            false,
        ),
        (
            "from .missing import pytest_configure\n",
            "def pytest_configure(config):\n    pass\n",
            false,
        ),
        (
            "from other.options import pytest_configure\n",
            "def pytest_configure(config):\n    pass\n",
            false,
        ),
    ] {
        std::fs::write(&entry, declaration).expect("write entry module");
        std::fs::write(&hooks, source).expect("write reexported module");
        assert_eq!(
            native_plugin_module_supported(&entry),
            admitted,
            "{declaration}"
        );
    }

    std::fs::write(&entry, "from .options import pytest_configure\n").expect("write entry module");
    std::fs::write(&hooks, "def pytest_configure(config):\n    pass\n").expect("write hook module");
    std::fs::create_dir(package.join("options")).expect("create ambiguous package");
    std::fs::write(package.join("options/__init__.py"), "")
        .expect("write ambiguous package module");
    assert!(!native_plugin_module_supported(&entry));
}
