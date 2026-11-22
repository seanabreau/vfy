# Upstream pytest corpus

The native corpus gate compares vfy with pytest 9.1.1 at [`cf470ec0bf7eb89cd97dd56df4859eae5db46447`](https://github.com/pytest-dev/pytest/tree/cf470ec0bf7eb89cd97dd56df4859eae5db46447). The [identity file](pytest-test-identity.json) pins that revision and the SHA-256 of the ordered case IDs. CI generates the source inventory and classifications from it and the reviewed [policy](pytest-test-policy.json). CI keeps those outputs as run artifacts.

## Comparison scope

The gate runs three clean copies of the pinned checkout:

| Run | Plug-in autoload | Cases |
| --- | --- | --- |
| Stock baseline | Enabled | All 4,231 |
| Stock reference | Disabled | 4,229 parity cases and one observed skip |
| Native candidate | Disabled | The same 4,230, in the same order |

This table describes the required runs, not a result for a particular vfy build. A successful `corpus-accounting.json` binds the comparison to its installed wheels and the upstream pytest revision.

All runs must exit successfully. The controlled pair uses the same arguments. It excludes the Hypothesis lifecycle case, whose native rejection is checked separately. The outer-xdist terminal case runs in both controlled lanes and skips, because those lanes disable outer plug-in autoload. Distributed xdist execution is covered by separate native integration tests. Accounting reports `full_parity: false` and `parity_scope: "supported-cases-only"`, even when the supported cases match.

Schema-2 records compare ordered case IDs, phases, outcomes, exceptions, case and session warnings, collected counts, and exit status. Raw diagnostics, stdout, stderr, and logs remain literal but are not compared for text parity. Compared fields normalize known checkout, interpreter, and temporary roots, including pytest's observed base temporary directory. Other paths, clocks, and memory addresses are unchanged.

Nested pytester projects still run stock pytest. In the controlled pair, nine named cases use subprocess mode to isolate their Hypothesis lifecycle. The remaining cases keep pytester's default in-process mode. The [shared configuration](https://github.com/seanabreau/vfy/blob/main/scripts/pytest_corpus_conftest.py) lists those exceptions. All nine stay in the supported comparison.

Explicit deselection disables native compact collection in this lane. [Focused integration tests](native-pytest-testing.md) cover unfiltered compact families, output rendering, and other native execution contracts.

## Run the corpus comparison

First complete the wheel build, environment installation, and upstream checkout steps in the [native pytest workflow](https://github.com/seanabreau/vfy/blob/main/.github/workflows/native-pytest.yml). Then run the commands below from the repository root. The rejection and full-corpus output directories must not already exist.

```console
reference_python="$PWD/target/native-pytest/reference/bin/python"
mkdir -p target/native-pytest/results
(cd target/native-pytest/upstream-pytest && \
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONDONTWRITEBYTECODE=1 \
    "$reference_python" -m pytest --collect-only -q --disable-warnings) \
    > target/native-pytest/results/pytest-collection.txt
.venv/bin/python scripts/pytest_corpus_inventory.py \
    --pytest-root target/native-pytest/upstream-pytest \
    --collect-log target/native-pytest/results/pytest-collection.txt \
    --identity-manifest docs/about/pytest-test-identity.json \
    --output target/native-pytest/results/pytest-test-suite.json
.venv/bin/python scripts/pytest_corpus_classification.py \
    --inventory target/native-pytest/results/pytest-test-suite.json \
    --policy docs/about/pytest-test-policy.json \
    --output target/native-pytest/results/pytest-test-classification.json
.venv/bin/python validation/pytest_runner/check_native_rejections.py \
    --stock-python "$PWD/target/native-pytest/reference/bin/python" \
    --candidate-python "$PWD/target/native-pytest/candidate/bin/python" \
    --upstream-root target/native-pytest/upstream-pytest \
    --classification target/native-pytest/results/pytest-test-classification.json \
    --pytest-artifact target/native-pytest/dist/pytest-9.1.1-py3-none-any.whl \
    --runner-artifact target/native-pytest/dist/vfy-*.whl \
    --output target/native-pytest/results/rejections
.venv/bin/python scripts/pytest_corpus_execution.py \
    --upstream-root target/native-pytest/upstream-pytest \
    --classification target/native-pytest/results/pytest-test-classification.json \
    --stock-python target/native-pytest/reference/bin/python \
    --candidate-python target/native-pytest/candidate/bin/python \
    --stock-runner-artifact target/native-pytest/dist/pytest-9.1.1-py3-none-any.whl \
    --stock-runner-artifact target/native-pytest/dist/vfy-*.whl \
    --candidate-runner-artifact target/native-pytest/dist/pytest-9.1.1-py3-none-any.whl \
    --candidate-runner-artifact target/native-pytest/dist/vfy-*.whl \
    --rejections target/native-pytest/results/rejections/rejections.json \
    --output target/native-pytest/results/full-corpus
```

The driver writes `corpus-accounting.json` only after all runs and rejection checks pass. The inventory and classification alone do not establish parity. The driver and the single-run recorder time out after 3,600 seconds by default. Set `--timeout` to change it.

## Run records

Completed runs retain raw and normalized execution records, command and status
metadata, ordered node IDs, and both output streams. Timed-out runs retain the
command, status, and streams. `command.json` records arguments, working directory,
and timeout.

Provenance records the upstream revision, inventory and policy hashes, interpreter, dependencies, wheel payloads, and observer sources. It also binds the loaded native adapter and extension to their installed wheel. It records the pytester exception IDs and the shared configuration hash. Missing or altered inputs fail validation.

## Pinned core collection

The default upstream configuration collects 74 Python modules and one text doctest file under `testing/`. The inventory keeps all 4,231 node IDs in order. Supporting pytester projects under `testing/example_scripts/` are inputs to these tests, not separately collected modules.

The inventory generator rejects a wrong revision, a dirty checkout, missing, duplicate, or reordered IDs, a count mismatch, and sources outside the map. Its digest is a compact JSON array of the ordered node IDs, followed by a UTF-8 newline. Review collection changes before you update the upstream pin. Static feature and import hints locate tests. They are not compatibility results.

## Corpus layers

The core comparison excludes these separate upstream lanes:

| Lane | Upstream entry point |
| --- | --- |
| Third-party plug-ins | `testing/plugins_integration/`, `tox.ini`'s `plugins` environment |
| Documentation doctests | `doc/en/`, `tox.ini`'s `doctesting` environment |
| Frozen executable | `testing/freeze/`, `tox.ini`'s `py310-freeze` environment |
| Optional dependencies and xdist | The corresponding `tox.ini` factors |

A compatibility result applies only to its recorded lane, interpreter, and [comparison scope](#comparison-scope). It does not establish native support for these separate lanes or for the nested stock pytest sessions.

## Cache behavior

Cache selection is tested separately through public native processes. See the
[cache checks](native-pytest-testing.md#find-a-test) for options and failure cases.
