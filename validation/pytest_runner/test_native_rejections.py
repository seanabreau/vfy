"""Proof helpers for native pytest's explicit third-party plug-in policy."""

import pytest
from validation.pytest_runner.check_native_rejections import (
    disabled_plugin_arguments,
    hypothesis_lifecycle_arguments,
    hypothesis_lifecycle_outer_failure_signature,
    hypothesis_lifecycle_summary_matches,
)


def test_hypothesis_lifecycle_probe_disables_every_other_pytest_entrypoint() -> None:
    arguments = disabled_plugin_arguments(
        ("hypothesispytest", "xdist", "xdist.looponfail", "vfy"),
        allowed=frozenset({"hypothesispytest"}),
    )

    assert arguments == [
        "-p",
        "no:vfy",
        "-p",
        "no:xdist",
        "-p",
        "no:xdist.looponfail",
    ]


def test_hypothesis_lifecycle_proof_rejects_unapproved_node() -> None:
    with pytest.raises(ValueError, match="unrecognized Hypothesis lifecycle node"):
        hypothesis_lifecycle_arguments("testing/test_junitxml.py::test_unrelated")


@pytest.mark.parametrize(
    ("target_nodeid", "outer_e_line", "expected_signature"),
    [
        (
            "testing/test_junitxml.py::test_random_report_log_xdist",
            "FileNotFoundError: [Errno 2] No such file or directory: '/tmp/pytest-1/junit.xml'",
            "FileNotFoundError: junit.xml",
        ),
        (
            "testing/test_terminal.py::TestTerminalFunctional::test_header_trailer_info",
            "Failed: remains unmatched: '*===== test session starts ====*'",
            "Failed: remains unmatched: '*===== test session starts ====*'",
        ),
    ],
)
def test_hypothesis_lifecycle_proof_requires_exact_outer_e_line(
    target_nodeid: str, outer_e_line: str, expected_signature: str
) -> None:
    stdout = f"E   {outer_e_line}\n"

    assert hypothesis_lifecycle_outer_failure_signature(stdout, target_nodeid) == (
        expected_signature
    )


def test_hypothesis_lifecycle_proof_rejects_changed_outer_e_line() -> None:
    with pytest.raises(RuntimeError, match="expected one exact outer failure signature"):
        hypothesis_lifecycle_outer_failure_signature(
            "E   Failed: remains unmatched: changed\n",
            "testing/test_terminal.py::TestTerminalFunctional::test_header_trailer_info",
        )


def test_passing_hypothesis_lifecycle_case_has_no_outer_failure_signature() -> None:
    assert (
        hypothesis_lifecycle_outer_failure_signature(
            "Captured stderr call\nHypothesisSideeffectWarning\n",
            "testing/test_terminal.py::TestTerminalFunctional::test_no_header_trailer_info",
        )
        is None
    )


def test_hypothesis_lifecycle_summary_uses_final_outer_line() -> None:
    assert hypothesis_lifecycle_summary_matches(
        "1 passed in 0.1s\n1 failed, 1 passed in 0.2s\n",
        expected_exit_status=1,
    )
    assert not hypothesis_lifecycle_summary_matches(
        "1 failed, 1 passed in 0.2s\n2 passed in 0.1s\n",
        expected_exit_status=1,
    )
    assert hypothesis_lifecycle_summary_matches(
        "1 passed in 0.1s\n2 passed in 0.2s\n",
        expected_exit_status=0,
    )
