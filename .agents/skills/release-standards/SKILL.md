---
name: release-standards
description: >-
  Use when reviewing vfy for a release, or when auditing a component's
  documentation, comments, diagnostics, CLI help, tests, repository layout,
  packaging, CI, or changelog against the standards of astral-sh/uv,
  astral-sh/ruff, and oven-sh/bun.
---

# Review vfy against release standards

These standards draw on uv, Ruff (including ty), and Bun. Judge each rule in
context against code and observable behavior. Word blacklists, sentence counts,
and finding quotas do not establish correctness or good writing.

Pinned reference material:

- [uv README](https://github.com/astral-sh/uv/blob/8f5ea4c01d0d488b785154aa2f98990b62f7b9a1/README.md)
  and [contributor guide](https://github.com/astral-sh/uv/blob/8f5ea4c01d0d488b785154aa2f98990b62f7b9a1/CONTRIBUTING.md).
- [Ruff engineering guidance](https://github.com/astral-sh/ruff/blob/b9959a107fb16eedc4a601abd80b066c45e03f0f/AGENTS.md).
- [Bun review guidance](https://github.com/oven-sh/bun/blob/2596ac5d391d47804c93a42bdbe615804aa62387/REVIEW.md).

Apply the practices to vfy's contracts; upstream commands, language choices,
repository layouts, and release conventions are examples rather than mandates.

`AGENTS.md` and the focused skills in `.agents/skills/` take precedence. This
skill is the review checklist; `writing`, `python`, `rust`, and `testing` own
the detailed guidance.

## README and landing pages

- Open with one factual sentence that says what vfy is (uv: "An extremely fast
  Python package and project manager, written in Rust."). Follow it with the
  first command, not a story.
- Keep the order short: tagline, one captioned figure, install and first
  commands, what else it does, documentation link, FAQ, contributing, license
  (uv `README.md`).
- Attach every number to a workload in its caption and link the methodology
  page (Ruff: "_Linting the CPython codebase from scratch._"; uv links
  `BENCHMARKS.md`). Captions must be readable by someone who has not read the
  benchmark page; move insider terms to the methodology page.
- Answer FAQ questions directly before explaining the qualification.
- No emoji feature lists, testimonials, or adjectives a measurement does not
  back.

## Documentation site

- Separate tutorials and guides (second person, imperative), concepts and
  architecture (third person, explanatory), and reference (exhaustive,
  third person) (uv `STYLE.md`; ty docs).
- Open each page with one sentence that states its subject or task. No
  "Welcome", no "This page will...", no table of contents in prose (uv, Bun
  `docs/test/*.mdx`).
- Put prerequisites before commands and the expected result right after them.
  Keep command blocks copyable without shell prompt prefixes. Put expected
  output in a separate block so copying a command does not copy its output.
  Keep output short and label excerpts (uv `STYLE.md`).
- State matching semantics, defaults, and limits as facts next to the option
  they qualify (Bun: "Filters are substring matches, not glob patterns.";
  "accepts a `statements` key but does not currently enforce it").
- Use admonitions sparingly: `note`, `tip`, `warning` only.
- Link text names its destination; never "here" (Bun contributing guide).
- Reference pages that mirror code (CLI flags, configuration keys, Python
  signatures) must match the implementation exactly. Prefer generating them;
  until then, verify every flag, default, and signature against the source.
- Every code example must run as written, or be labelled partial.

## Prose voice

Apply to docs, docstrings, comments, diagnostics, changelog, and PR text.

- Short sentences, one point each. Active voice with a named actor. Present
  tense for current behavior; no "will" for current behavior (Bun "Voice").
- Address the reader as "you"; avoid "we" and "let's" (uv `STYLE.md`).
- Remove empty introductions, unsupported praise, and unnecessary qualifications
  when they obscure the reader's task (uv, Bun).
- Write for the eventual reader: no discarded alternatives, intermediate edits,
  session history, branch status, or review diaries (Ruff and uv `AGENTS.md`).
- No past-facing wording ("now", "previously", "preserve the existing
  behavior") unless an external compatibility requirement needs it (uv).
- Replace marketing language with the specific guaranteed property (Bun).
- Do not invent jargon when a term of art exists; define necessary terms
  before using them (Ruff mdtest rules).
- One canonical home per fact; link to it instead of repeating it.

## Comments and docstrings

- Comment invariants and unusual choices, not the next line (Ruff
  `AGENTS.md`: "Don't use comments to narrate code"). A comment must make
  sense to someone reading the code for the first time.
- Fix an avoidable workaround at its owning boundary. Preserve explanations of
  real FFI, ownership, version, failure, and compatibility constraints.
- A comment that contradicts the code is a bug (Bun `landing-prs.md`).
- Public Rust items: one imperative sentence (`/// Return the cache entry's
  parent directory.`), types as noun phrases, a second paragraph only for
  behavior, errors, or side effects (uv).
- Python docstrings state the contract a caller cannot infer from the
  signature. Do not paraphrase the function name.
- Every lint suppression has a local reason; prefer `#[expect]` over
  `#[allow]` (uv and Ruff `AGENTS.md`). Remove dead code instead of silencing
  dead-code lints.
- `expect("...")` messages state the invariant (uv: `.expect("Symlink path
  must have a parent")`).

## Diagnostics and CLI help

Apply these conventions to vfy's own messages. Preserve Coverage.py and pytest
formats where the documented compatibility contract requires them.

- Name what failed and why: the resource (quoted path), the rejected value,
  the cause, and a concrete remedy (Bun `REVIEW.md`).
- Sentence case, no trailing period on single-sentence messages, code in
  backticks or quotes; typical form "Failed to <verb> <object>" (uv). Put
  recovery advice on a separate `hint:` or `note:` line (uv, Bun).
- Use "failed to <verb>" when an operation hit an error and "cannot <verb>
  ...; <remedy>" for an unmet precondition the user must fix. State the failed
  operation or unmet precondition; do not select wording by counting messages.
- Where vfy replaces an upstream call (for example pytest's `open(logfile)`),
  raise the exception type upstream would raise, and prove it with a
  differential test.
- Errors and logs go to stderr; stdout carries only pipeable output (uv).
- Never "Please". Use public option names, not internal field names (Bun).
- Requested operations fail the command on any failure; do not warn and exit 0
  (Bun).
- CLI help starts with a concrete verb phrase; later paragraphs give defaults,
  platform behavior, and flag interactions (uv `uv-cli`). Test option exposure and real workflows;
  do not add tests that merely restate preferred wording.

## Tests

- A behavioral fix needs a regression that fails before the fix for the intended
  reason (Bun `REVIEW.md`). An existing invariant test need not fail for an
  unrelated refactor or prose edit.
- Test through the public boundary: the real CLI or public API in an isolated
  temporary project (uv `TestContext`, Bun `bunExe()`/`tempDir`, Ruff
  `CliTest`). In vfy this is `tests.support.vfy_project.VfyProject`.
- Assert exit status, relevant stream contents, and side effects. Use exact
  output or a reviewed snapshot when the complete output is the contract.
  Normalize only machine-specific data outside that contract, reusing helpers
  for actual shared needs (uv `INSTA_FILTERS`).
- Never hand-edit snapshots; regenerate, then read the diff (Ruff).
- Establish nonempty inputs and assert every required case. Conditional
  expectations must follow the actual platform or pinned oracle capability;
  they must not silently omit the behavior under review (Bun).
- No sleeps to wait for a condition; poll with a deadline or wait on the event.
  Use port 0. No public network access (Bun).
- Cover the variant matrix that the change affects: every entry point, both
  states of a flag, limit boundaries, error paths, and the negative contract
  (nothing written on failure). Do not repeat a test for every alias or
  spelling (uv `AGENTS.md`).
- Name tests after the behavior they protect (uv `version_get_json`, Ruff
  `exclude_stdin`). Add to the existing owning file before creating a new one
  (Bun).
- Do not test static values, removed features, or preferred wording.
- Never skip, weaken, or delete a test without a stated reason.

## Repository layout and packaging

- Keep the root to metadata, configuration, and top-level docs (uv).
- Crates share the product prefix and are named by role; CONTRIBUTING lists
  each crate with a one-line role ("library crate for ...") (Ruff, uv).
- Internal crates say so in `description` and set `publish = false` unless
  released (uv, Ruff).
- Workspace metadata and lints are inherited (`[lints] workspace = true`).
  Configure lints once in `[workspace.lints]`; give every allow-list entry a
  reason (Ruff, uv).
- Ban APIs that bypass an invariant (for example raw filesystem writes that
  skip atomic replacement) with `clippy.toml` `disallowed-methods` and a reason
  (uv, Ruff).
- The shipped Python package contains product code only. Development harnesses,
  experiments, and benchmark data stay out of the wheel and sdist.
- `scripts/` holds single-purpose scripts named for their task.
- Split a module along a concept boundary, not for line count alone. Keep
  related invariants and ownership together; upstream file lengths are not
  size targets or exemptions from review.
- Sibling modules inside `vfy` may import private helpers. A leading underscore
  marks a non-public API by convention; it does not enforce a privacy boundary.
  Keep imports of upstream private modules (`_pytest`,
  pluggy, Hypothesis internals) to what compatibility requires.
- Compute environment facts such as installed plug-ins once per operation and
  pass them to the code that needs them. A process-wide memo goes stale in
  long-lived processes like `vfy test --watch`.
- Experimental or exploratory code is either maintained and tested, or removed
  before release. Retained records state the revision and workload they
  measured.

## Benchmarks

- Publish only results from the committed harness, with workload revision,
  command, warmup, sample count, statistic, spread, and machine (Ruff
  `CONTRIBUTING.md` "CPython Benchmark"; uv `BENCHMARKS.md`).
- State caveats up front ("Benchmark performance may vary dramatically across
  different operating systems and filesystems.").
- Compare against a saved baseline, never a best single run (Ruff).
- Make the harness runnable from documented commands (Bun `bench/README.md`).

## CI and release

- Make required checks and their aggregate status clear (uv, Ruff
  `required-checks-passed`). Keep separate workflows when their triggers,
  environments, or ownership require it.
- `permissions: contents: read` by default, `persist-credentials: false`,
  actions pinned by SHA with a version comment, `timeout-minutes` on every job,
  `concurrency` with cancel-in-progress, and a workflow security linter
  (uv `check-zizmor.yml`).
- Fail CI when generated files or docs drift from the source.
- Release and publish workflows are separate from CI.
- Release steps are documented and scripted; tag only a verified commit.

## Changelog

- Newest version first, `## <version>` then `Released on YYYY-MM-DD.` (or
  Unreleased), then fixed sections (uv, Ruff).
- Each entry is one imperative line about user-visible behavior with code in
  backticks and, once public, a PR link. Performance entries name the
  mechanism, not an adjective (Ruff: "Use a Bloom filter for formatter comment
  lookups").
- No implementation diary, emoji, or thanks.
- A first release describes what the release contains; it does not narrate
  development history.

## Running a review

1. Split the tree into components with disjoint file ownership.
2. For each component, check every applicable section above, verifying claims
   against code rather than wording.
3. Fix what is clearly wrong; record structural or behavioral issues that need
   a decision.
4. Run the owning checks from `docs/about/testing.md`, then the full checklist
   in `CONTRIBUTING.md` before handoff.
5. Keep review reports out of the repository.
