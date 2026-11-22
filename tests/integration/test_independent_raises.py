"""Public exception matching agrees with the pinned pytest implementation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import VfyProject
from tests.support.platforms import requires_native_pytest

pytestmark = requires_native_pytest

_EXERCISE = r"""import importlib
import json
import re
import sys
import warnings
from typing import Any

api = importlib.import_module(sys.argv[1])
rows = []


def capture(label, operation):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            result = operation()
            rows.append([label, "ok", result])
        except BaseException as error:
            rows.append([label, type(error).__name__, str(error)])
        rows.append(
            [label, "warnings", [[type(w.message).__name__, str(w.message)] for w in caught]]
        )


class Check:
    def __init__(self, result):
        self.result = result

    def __call__(self, error):
        return self.result

    def __repr__(self):
        return "Check(" + repr(self.result) + ")"


class ExplodingCheck:
    def __call__(self, error):
        raise LookupError("check exploded")

    def __repr__(self):
        return "ExplodingCheck()"


def constructor(expected, **kwargs):
    matcher = api.RaisesExc(expected, **kwargs)
    return [
        repr(matcher),
        [t.__name__ for t in matcher.expected_exceptions],
        matcher.is_baseexception,
    ]


if sys.argv[2] == "construction":
    for name, expected in [
        ("empty", None),
        ("type", ValueError),
        ("tuple", (ValueError, TypeError)),
        ("invalid_class", str),
        ("instance", ValueError("bad")),
        ("number", 1),
        ("empty_tuple", ()),
        ("base", KeyboardInterrupt),
        ("group", ExceptionGroup[Exception]),
        ("basegroup", BaseExceptionGroup[BaseException]),
        ("anygroup", ExceptionGroup[Any]),
        ("badgroup", ExceptionGroup[ValueError]),
    ]:
        capture(name, lambda expected=expected: constructor(expected))
    for name, pattern in [
        ("plain", "message"),
        ("compiled", re.compile("message")),
        ("flagged", re.compile("message", re.I)),
        ("empty", ""),
        ("invalid", "["),
    ]:
        capture("pattern_" + name, lambda pattern=pattern: constructor(ValueError, match=pattern))
    capture("check_only", lambda: constructor(None, check=Check(True)))
    capture("invalid_order", lambda: constructor(str, match="["))
    capture("unknown_kw", lambda: api.raises(ValueError, extra=True))

elif sys.argv[2] == "matching":
    errors = [
        None,
        ValueError("needle"),
        TypeError("needle"),
        ValueError("other"),
        ExceptionGroup("needle", [ValueError("inner")]),
    ]
    for name, kwargs in [
        ("type", {}),
        ("pattern", {"match": "needle"}),
        ("check_false", {"check": Check(False)}),
        ("check_true", {"check": Check(1)}),
        ("check_explodes", {"check": ExplodingCheck()}),
        ("check_invalid", {"check": 42}),
        ("check_skipped", {"match": "nomatch", "check": ExplodingCheck()}),
    ]:
        matcher = api.RaisesExc(ValueError, **kwargs)
        for i, error in enumerate(errors):

            def match(error=error):
                result = matcher.matches(error)
                return [result, matcher.fail_reason]

            capture(name + str(i), match)
    error = ValueError("message")
    error.add_note("note needle")
    matcher = api.RaisesExc(ValueError, match="needle")
    capture("notes", lambda: [matcher.matches(error), matcher.fail_reason])
    for expected in [ExceptionGroup, BaseExceptionGroup]:
        matcher = api.RaisesExc(expected, match="inner")
        error = ExceptionGroup("outer", [ValueError("inner")])
        capture(expected.__name__, lambda: [matcher.matches(error), matcher.fail_reason])

elif sys.argv[2] == "contexts":
    for name, expected, pattern, raised, check in [
        ("success", ValueError, "needle", ValueError("needle"), Check(True)),
        ("wrong_type", ValueError, None, TypeError("bad"), None),
        ("no_error", ValueError, None, None, None),
        ("no_error_tuple", (ValueError, TypeError), None, None, None),
        ("no_error_any", None, "needle", None, None),
        ("regex_error", ValueError, "needle", ValueError("other"), None),
        ("check_error", ValueError, None, ValueError("needle"), Check(False)),
    ]:

        def context():
            with api.raises(expected, match=pattern, check=check) as info:
                if raised is not None:
                    raise raised
            return [info.type.__name__, str(info.value), info.match("needle")]

        capture(name, context)
    for pattern in ["needle", "missing", re.compile("missing"), "^needle$", "needle["]:

        def info_match():
            with api.raises(ValueError) as info:
                raise ValueError("needle")
            return info.match(pattern)

        capture("info_" + repr(pattern), info_match)

    def callable_case():
        def fail(**kwargs):
            raise ValueError(repr(kwargs))

        info = api.raises(ValueError, fail, match="forwarded", check="forwarded")
        return str(info.value)

    capture("callable_keywords", callable_case)

elif sys.argv[2] == "text_diffs":
    for i, (expected, actual) in enumerate(
        [
            ("needle", "other"),
            ("a.b", "axb"),
            ("", "nonempty"),
            ("one\ntwo", "one\nthree"),
            (" ", "\t"),
            ("\n\n", "\r\n"),
            ("same " * 20 + "x", "same " * 20 + "y"),
            ("b" * 50 + "x" + "a" * 100, "b" * 50 + "y" + "a" * 100),
            ("b" * 50, "b" * 51),
            ("x" + "a" * 100, "longer" + "a" * 100),
            ("café", "cafe"),
            ("a\r\nb", "a\r\nc"),
            ("x\n", "x\r\n"),
        ]
    ):
        matcher = api.RaisesExc(ValueError, match="^" + re.escape(expected) + "$")
        capture(str(i), lambda: [matcher.matches(ValueError(actual)), matcher.fail_reason])

elif sys.argv[2] == "callback_order":
    events = []

    class Error(ValueError):
        @property
        def __notes__(self):
            events.append("notes")
            return ["extra"]

        def __str__(self):
            events.append("str")
            return "needle"

    class Callback:
        def __call__(self, error):
            events.append("check")
            return True

        def __repr__(self):
            events.append("check_repr")
            return "Callback()"

    matcher = api.RaisesExc(ValueError, match="needle", check=Callback())
    capture("order", lambda: [matcher.matches(Error()), list(events)])

    class Recursive:
        def __call__(self, error):
            if str(error) == "outer":
                return not matcher.matches(TypeError("inner"))
            return True

    matcher = api.RaisesExc(ValueError, check=Recursive())
    capture("recursive", lambda: [matcher.matches(ValueError("outer")), matcher.fail_reason])
    original = re.compile

    def broken(*args, **kwargs):
        raise MemoryError("compile exploded")

    re.compile = broken
    try:
        capture("compile_other_error", lambda: api.RaisesExc(ValueError, match="needle"))
    finally:
        re.compile = original

elif sys.argv[2] == "line_alignment":
    import random
    random_source = random.Random(17)
    for index in range(200):
        expected = ''.join(random_source.choice('ab \n\t')
                           for _ in range(random_source.randrange(1, 30)))
        actual = ''.join(random_source.choice('ab \n\t')
                         for _ in range(random_source.randrange(1, 30)))
        matcher = api.RaisesExc(ValueError, match='^' + re.escape(expected) + '$')
        capture(str(index), lambda: [matcher.matches(ValueError(actual)), matcher.fail_reason])

elif sys.argv[2] == "lifetime":
    import gc
    import threading
    import weakref

    class CyclicCheck:
        def __call__(self, error):
            return self.matcher.matches(TypeError("nested")) is False
    def cycle():
        check = CyclicCheck()
        matcher = api.RaisesExc(ValueError, check=check)
        check.matcher = matcher
        refs = [weakref.ref(check), weakref.ref(matcher)]
        return refs
    refs = cycle()
    gc.collect()
    rows.append(["cycle", [ref() is None for ref in refs]])
    matcher = api.RaisesExc(ValueError, match="needle", check=Check(True))
    result = []
    thread = threading.Thread(target=lambda: result.append(matcher.matches(ValueError("needle"))))
    thread.start()
    thread.join()
    rows.append(["thread", result])

print(json.dumps(rows))
"""


@pytest.mark.parametrize(
    "category",
    [
        "construction",
        "matching",
        "contexts",
        "text_diffs",
        "callback_order",
        "line_alignment",
        "lifetime",
    ],
)
def test_raises_matcher_matches_pinned_pytest(tmp_path: Path, category: str) -> None:
    project = VfyProject(tmp_path)
    project.write("exercise.py", _EXERCISE)
    stock = project.run_python("exercise.py", "pytest", category)
    candidate = project.run_python("exercise.py", "vfy.independent.facade", category)
    stock.expect(0)
    candidate.expect(0)
    assert json.loads(candidate.stdout) == json.loads(stock.stdout)
