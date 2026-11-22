# vfy engineering invariants

Treat every file under `sources/` as read-only reference material. Do not
edit, rename, move, or delete synced project files; they may be replaced when
the ChatGPT project is synchronized.

vfy is a Rust-first Coverage.py-compatible engine with a thin Python
compatibility shell. Keep collection, data, analysis, selection, scheduling,
aggregation, rendering, and atomic filesystem commits in Rust. Python owns
CPython/pytest lifecycle hooks, public compatibility adapters, stdout, and
third-party plug-in calls only.

Preserve these invariants:

- Do not put Python on the ordinary monitoring-event path.
- Keep one authoritative Rust data and report model; do not add Python
  fallbacks or duplicate algorithms.
- Optimize the test engine by removing work or improving data ownership;
  do not add caches or memoized fast-path eligibility.
- Validate all inputs before mutation and preserve atomic replacement on
  failure.
- Preserve Coverage.py and pytest signatures, exit statuses, exceptions,
  streams, schema-7 behavior, and documented side effects.
- Keep persisted and public ordering deterministic.
- Every compatibility change needs a differential or round-trip proof against
  the pinned reference where one exists.
- A performance claim requires semantic parity first and the committed
  benchmark harness; do not publish a best-run result.
- New production lint suppressions require a local reason. Prefer a named
  enum or options struct over ambiguous Boolean or `Option` arguments.
- Do not add tests for statically defined values or for behavior that was
  removed; tests must protect a user-visible contract or a Rust invariant.
- Collector, process, scheduler, and pytest-lifecycle changes require a real
  public-boundary integration test through `tests.support.vfy_project.VfyProject`.
- Independent-engine tests must invoke `--engine=native` explicitly.
  `--engine=pytest-native` uses installed pytest for collection and vfy for execution; a test name
  or a native extension import does not establish which engine executed.

Use the focused instructions in `.agents/skills/` for Python, Rust,
compatibility, performance, and testing work. When a review failure recurs,
record the rule here or in the relevant skill first; promote objective rules to
an architecture test, lint, or CI check once their wording is stable.

Use `.agents/skills/release-standards/SKILL.md` to review a component or the
repository before a release.

Use `.agents/skills/writing/SKILL.md` for documentation, docstrings, comments, and
user-facing diagnostics. Describe the first release in present tense; do not
invent vfy migrations or release history. Use literal, task-specific command
names and neutral diagnostics. Avoid playful metaphors, novelty aliases,
exclamations, and decorative emoji in product copy. During review:

- Verify paths, commands, defaults, and unsupported options against the final
  tree. Preserve the recorded revision and workload of historical results.
- Describe what validation actually checks; do not turn a configured CI job or
  an old corpus run into a claim that the current head passed.
- Use diagram arrows for real dependencies, branches, or event order. Use a
  table for independent test layers and remove diagrams that repeat a list.
- Fix a broken contract instead of writing a comment that excuses it. Keep
  compatibility, failure, and lint explanations that identify a real constraint.

Run the narrowest owning test while iterating, then the complete affected
layer. Before handoff, use the repository checks documented in
`docs/about/testing.md` and `CONTRIBUTING.md`.
