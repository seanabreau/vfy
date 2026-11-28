# How vfy runs pytest plug-ins

A project moving to vfy should be able to keep its lockfile, tests, and
`conftest.py`, change the runner command, and see which behavior carries over.
The first comparison is `pytest tests -q` and `vfy test --no-cov tests -q` in
the same environment. On Linux and macOS, the latter uses vfy’s native engine.
A green run is meaningful only if it collected the intended tests and performed
the setup, execution, and teardown the suite depends on.

## Decide who supplies the behavior

Make this decision for each operation. A single package can supply several.

| What the suite uses | What the native runner should do | Example |
| --- | --- | --- |
| A feature vfy implements | Run the same native code whether the distribution is absent, present, or upgraded. Report installed metadata when the public API exposes it. | An async test awaits its coroutine, shares the requested loop, and tears down its async fixture. Installing pytest-asyncio 1.5.0 cannot disable that. |
| An installed fixture or hook | Run the original provider when the engine supports its lifecycle. Preserve fixture ownership, hook order, exceptions, and return values. | pytest-randomly controls the seed. A worker must receive that seed and reproduce stock collection order. |
| A feature with both native and upstream parts | Run the native part directly. Check each required upstream interface where the operation uses it. | Calling an unmeasured CodSpeed fixture can run the callable once. Its pedantic setup uses an installed helper. Actual measurement needs the upstream runner. |
| An unsupported active operation | Stop at the earliest point where the engine knows it cannot honor the request. Name the option, hook, or fixture and give a command that can run it. | With `--codspeed`, direct the user to `vfy test --engine=pytest --codspeed` before any tests execute. |

For example, the native CodSpeed fixture accepts one invocation per test.
Stock pytest-codspeed 3.2.0 permits repeated calls. A project relying on that
older behavior can keep both calls and use `vfy test --engine=pytest`.

### Resolve a provider once

1. Honor the selected engine, explicit plug-in requests, and `-p no:NAME`.
   Preserve the route’s autoload settings. Disabling a provider must also
   disable its aliases.
2. When vfy substitutes for a named upstream entry point, check its owner and
   target. A third party can register the same name. Substituting vfy’s code
   there would silently discard that party’s hooks.
3. If a real provider registered, its fixtures and hooks keep their ownership.
   When the route supplies a built-in provider, an installed distribution
   without a matching entry point must not make that provider disappear.
4. An active hook can affect the suite without appearing in a test body.
   A collector can add tests, an ordering hook can change the seed, and a
   timeout can turn a hang into a failure. Skipping that work or changing
   engines automatically can produce a false pass. Use `--engine=pytest`
   explicitly to run a suite that requires the upstream lifecycle.

### Put version checks at actual dependencies

An exact development pin identifies the upstream behavior a test compared.
Keep it in the lockfile or reference environment. The native asyncio runner
implements its own fixture and event-loop lifecycle, so that pin has no
place in deciding whether a user's test may run.

When vfy invokes upstream code, use the interface it actually calls to judge
compatibility. Some boundaries require more than checking that a method
exists. For example, the installed xdist controller and workers must agree on
collection IDs and the order of reports. A bounded version range is justified
when it describes a tested protocol. Record the boundary, exercise its ends
with real distributions, and tell users which engine can run a different
protocol. A failure of an optional optimization should take the ordinary
execution path when that path preserves the same behavior.

## What counts as migration evidence

Start with a downstream project’s test dependencies intact. Run stock pytest
and the candidate wheel at the same project revision with the same selectors,
configuration, random seed, and worker count. Compare ordered node IDs for
serial or deterministic collection, outcomes by node ID, exit status, and
the side effect the plug-in owns. For parallel execution, compare worker
inventories and per-test phases. Completion order can legitimately vary.

For Structlog, that means running its async suite and exercising fixture
teardown on reference and current pytest-asyncio releases. For an attrs
benchmark, an unmeasured call must execute the benchmark body and return its
result. A run requested with measurement must produce measurements or fail.
For Platformdirs, inspect the fixture’s temporary directories and their
restoration after the test. A successful import proves none of these.

Keep a small public-command regression for each failure alongside the upstream
comparison. Use a deliberately unsupported operation to check the diagnostic
and whether any test side effects occurred. Once the runs agree on behavior,
measure performance on the same workload.

The [compatibility matrix](compatibility.md) records implemented features and
known limits by engine. The [adoption guide](../guides/native-adoption.md)
gives the commands to compare a project today. The optional
`--engine=pytest-native` route uses installed pytest for collection and has its
own plug-in policy. Its diagnostics report omitted plug-ins.
