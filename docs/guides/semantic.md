# Semantic decision coverage

Find untested conditions in expressions such as `x >= 0 and x < 10`.
Semantic coverage shows which comparisons were true or false and whether each
could change the result. Use it to choose another test input or fail a CI check.

!!! note "Alpha workflow"

    Requires CPython 3.12+. It turns on branch collection and is a vfy extension,
    so put its settings under `[tool.vfy]`, not `[tool.coverage]`.

<a id="fix-a-gap"></a>

## Find a missing test input

Use a separate uv project with [vfy and pytest installed](../getting-started/installation.md)
in its environment. Create these two files in the project root. No additional
pytest or vfy configuration is needed.

`bounds.py`:

<!-- doc-test: semantic-source -->
```python
def in_bounds(x):
    if x >= 0 and x < 10:
        return True
    return False
```

`test_bounds.py`:

<!-- doc-test: semantic-tests -->
```python
from bounds import in_bounds


def test_below_lower_bound():
    assert in_bounds(-1) is False


def test_inside_bounds():
    assert in_bounds(5) is True
```

Run pytest with semantic collection and per-test contexts:

<!-- doc-test: semantic-collect -->
```console
uv run pytest -p no:pytest_cov -p vfy.pytest_plugin --cov=bounds.py --cov-context=test --cov-decisions test_bounds.py
```

This uses stock pytest on Linux or macOS. The coverage plug-in replaces
pytest-cov and leaves pytest-html and pytest-metadata enabled. It does not require
the native test engine. See [collection](collection.md) for plug-in loading details.

Both tests pass and line/branch coverage is **100%**, but the semantic report shows:

| Metric | Covered | Percentage |
| --- | --- | --- |
| Decision outcomes | 2/2 | 100% |
| Condition values | 3/4 | 75% |
| MC/DC conditions | 1/2 | 50% |

The tests take both branches of the `if`, so branch coverage is complete. But
`x = -1` skips `x < 10`, and `x = 5` makes both comparisons true. Neither test
makes `x < 10` false. The recorded vectors are `0-` and `11`.

Check the saved `.coverage` file with a strict gate:

<!-- doc-test: semantic-fail -->
```console
uv run vfy coverage check --strict
```

The report identifies the missing comparison result:

```text
missing: 'x < 10' must be false (2:19)
missing: 'x < 10' needs an independent effect pair (2:19)
```

This intentionally exits **2**: condition coverage is 75% and MC/DC is 50%, below
the strict gate's 100% thresholds. It reads saved observations without rerunning tests.

Append this test to `test_bounds.py`:

<!-- doc-test: semantic-missing-test -->
```python
def test_at_upper_bound():
    assert in_bounds(10) is False
```

Recollect before checking again:

<!-- doc-test: semantic-complete -->
```console
uv run pytest -p no:pytest_cov -p vfy.pytest_plugin --cov=bounds.py --cov-context=test --cov-decisions test_bounds.py
uv run vfy coverage check --strict
uv run vfy explain bounds.py:2
```

All **three tests pass**. The added `10` observation brings decision, condition,
and MC/DC coverage to **100%**; the strict gate exits **0**. `explain` lists the
recorded tests and pairs of observations showing each condition changing the
outcome.

The semantic report suggests a recorded test that reaches the decision. On Linux
or macOS, copy the `vfy test` command printed below `try:` to rerun that node ID.
Use the test to find where to add the missing input. With stock pytest, pass the
same node ID to `pytest`.

Durations, when present, come from `.vfy/test-history.sqlite3`. Use `--history PATH`
to read a different file. Test suggestions require recorded test contexts.
Changed source requires fresh collection before checking its saved observations.

## Understand the counts

In `if x >= 0 and x < 10`, the whole expression is a **decision**;
`x >= 0` and `x < 10` are its **conditions**. vfy checks:

- **Decision coverage:** the `if` was true once and false once.
- **Condition coverage:** each condition was true once and false once.
- **Masking MC/DC** (modified condition/decision coverage): a pair of runs shows
  each condition changing the outcome. Other conditions stay fixed or cannot
  affect the result, including conditions Python skipped through short-circuiting.

MC/DC does not require every combination of condition values; that is
multiple-condition coverage. It also does not check every numeric boundary.

Each run records a vector of condition values. `-` means `and` short-circuited
and Python didn't evaluate the condition:

| Input | `x >= 0` | `x < 10` | Vector | Decision |
| --- | --- | --- | --- | --- |
| `-1` | false | not evaluated | `0-` | false |
| `10` | true | false | `10` | false |
| `5` | true | true | `11` | true |

`0-` and `11` show that `x >= 0` changes the result; the skipped second
condition cannot affect the false outcome. `10` and `11` keep `x >= 0` true
while `x < 10` changes the result. Without `10`, that second pair is missing.

These three inputs cover the conditions, not every value or boundary. For
example, they never check `x = 0`: changing `>=` to `>` would leave all three
tests passing. Check that boundary with:

<!-- doc-test: semantic-boundary-test -->
```python
def test_at_lower_bound():
    assert in_bounds(0) is True
```

[Mutation testing](mutation.md) can check whether tests detect such a change.

Boolean decisions get condition vectors and MC/DC. `match`, exception dispatch,
cancellation regions, and value selection report named outcomes instead.

## Check saved coverage or fail CI

`uv run vfy coverage check --data-file .coverage` lists gaps in saved coverage.
Like `explain`, it does not rerun tests. Add `--strict` to require 100% decision,
condition, and MC/DC coverage; explicit thresholds override those defaults.

To fail the run below a threshold:

```console
uv run pytest -p no:pytest_cov -p vfy.pytest_plugin --cov=bounds.py --cov-decisions \
    --cov-fail-under-decisions=95 --cov-fail-under-mcdc=85
```

Semantic thresholds are independent of `--cov-fail-under`.

If collection and reporting run in separate CI jobs, check the saved data:

```console
uv run vfy coverage check --data-file .coverage --format json \
    --output build/decisions.json --fail-under-mcdc 85
```

`vfy coverage check` exits `0` when every threshold passes, `2` when one fails,
and `1` when the data is missing, corrupt, or stale.

## Exclusions

Existing Coverage.py pragmas apply to the decision they sit on:

```python
if debug_only:  # pragma: no cover
    emit_debug_data()

if intentionally_partial:  # pragma: no branch
    recover()
```

To leave a decision out of semantic coverage only:

```python
if platform_specific:  # pragma: no decision
    use_platform_feature()
```

`exclude_also` and `partial_also` patterns apply too. To exclude by expression or
kind at check time, repeat `--exclude-decisions`:

```console
uv run vfy coverage check --exclude-decisions 'platform_specific|match_guard'
```

## Save or configure reports

The semantic report defaults to terminal output. To write JSON during collection,
add `--cov-decision-report=json:build/decisions.json` to the first command.
The Alpha JSON shape may change; see the [semantic API reference](../reference/python-api.md#semantic-report).

Reports fail with the affected path if a discovered source file cannot be read
or parsed, or a source directory cannot be listed.

For persistent settings, use `[tool.vfy.run] decisions = true` and
`[tool.vfy.check]` thresholds. Command-line flags override them. See
[configuration](../reference/configuration.md#toolvfycheck) for accepted values.

## Limits

- Coverage.py file-tracer plug-ins are rejected in semantic mode with an error.
  Dynamic-context plug-ins work.
- Opt-in. With decisions off, no semantic data is created.
- Observations are tied to the source digest: changed source requires new
  collection. xdist and subprocess observations combine under the same rules;
  conflicting digests or failed validation leave saved coverage unchanged. See
  [data files](../reference/data-files.md#semantic-tables).

For line and branch compatibility, see [Compatibility](../about/compatibility.md).

## Appendix: supported decisions

| Form | Example |
| --- | --- |
| `if` / `elif` | `if ready and valid:` |
| `while` | `while item and not done:` |
| `assert` | `assert response.ok` |
| conditional expression | `value = left if ready else right` |
| comprehension filter | `[item for item in values if item.active]` |
| `match` case selection | `case [x, y]: ...` (one named outcome per case) |
| `match` guard | `case [x, y] if x < y:` |
| exception dispatch | `try: ... except ValueError: ...` |
| cancellation-aware async region | `try: await work() finally: cleanup()` |
| value selection | `return cached or compute()` |

- A `match` also reports `no case matched` unless it has an unguarded wildcard or
  capture case.
- Value selection reports which operand supplied the result. vfy doesn't call
  `bool()` again or change behavior.
- Cancellation outcomes exist only for explicit cleanup or cancellation handling,
  not every `await`.
- A bare `raise` outside a `try` is not a decision.
