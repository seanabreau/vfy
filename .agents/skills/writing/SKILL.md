---
name: writing
description: >-
  Use when writing or reviewing vfy documentation, release notes, code
  comments, docstrings, or user-facing diagnostics.
---

# Write vfy documentation and comments

vfy is preparing its first release. Describe supported behavior in present
tense; do not invent prior releases, migrations, or command-renaming history.
Keep the Unreleased marker until publication and preserve the Alpha status.
Retained benchmark records must keep their measured commands and revisions,
even when the documented command differs.

## Choose what belongs here

| Location | Reader needs | Leave elsewhere |
| --- | --- | --- |
| README and home | Capability, highlights, first command, brief requirements and Alpha status | Benchmark methodology, detailed limits, module tours, release checklists |
| Guide | Prerequisites, runnable workflow, result, next action | Exhaustive flags and engine internals |
| Reference | Accepted values, defaults, side effects, errors, exceptions | Repeated tutorials |
| Architecture | Ownership, invariants, tradeoffs, failure behavior | A prose inventory of every module |
| Benchmark record | Workload, parity, commands, revisions, samples, statistic | Unmeasured product claims |
| Changelog | User-visible capability or correction | Implementation diary |
| API docstring | Contract a caller cannot infer from the signature | A paraphrase of the function name |
| Code comment | Why this choice is necessary; what breaks if changed | A translation of the adjacent statements |

Each fact needs a canonical home. Link to it, retaining a local limit when
omitting it would make a command misleading or unsafe.

Keep the README and home easy to scan. Use one focused benchmark chart with
enough comparisons to be useful, a short workload caption, and a methodology
link. Put sampling, engine options,
replay setup, and workflow-specific limits in the benchmark page or owning guide.
State Alpha status once per overview; do not repeat it for each feature.
Use transparent, theme-aware SVG charts in the docs. Check manual theme switches
independently of the system color scheme. For private GitHub READMEs, prefer
repository-relative Markdown images so GitHub handles access to the asset.
Export single-palette PNG charts with an opaque background so their text stays
readable in either GitHub theme.
An authenticated raw download or a local preview does not prove that GitHub's
rendered page or a public package description can load a private image. Verify
those boundaries separately; package descriptions need public asset hosting.
Keep the inline FAQ, including pronunciation, when shortening the overview.

## Explain the task

Lead with what the user can do, show the command or API, and state relevant
limits. Keep architecture details in `docs/about/architecture.md` unless they
help a user make a decision. Link to a canonical workflow instead of repeating
it across sections. Explain installation requirements on the installation page;
do not make the absence of dependencies a recurring product claim.

Charlie Marsh's [uv announcement](https://astral.sh/blog/uv) and
[Ruff formatter announcement](https://astral.sh/blog/the-ruff-formatter) lead
with capability and concrete examples, then explain adoption and technical
choices. [Ruff's reference](https://docs.astral.sh/ruff/formatter/) keeps detailed
behavior close to the option it qualifies. Follow those structures without
copying wording or borrowing performance claims.

Replace feature-relative words such as “now” and “previously” with the behavior
itself. Delete praise, vague introductions, PR diaries, and explanations of
abandoned implementations. Review meaning in context; a vocabulary blacklist
does not establish correctness or good writing.

For performance writing, report what a reproducible benchmark measured rather
than presenting aspirational ratios as release targets, acceptance criteria,
promises, or roadmap milestones. Name an enforced regression threshold only
when readers need it to run or interpret a real CI check, and describe it as a
check for its specific workload. Do not turn internal benchmark thresholds into
the story of the product.

Use headings to name the reader's task or the section's subject. Replace
ornamental symmetry such as “Three questions, three commands,” vague journey or
direction headings, and decorative “from X to Y” slogans with the concrete
topic. Rewrite “more than X,” “not just X,” and “there is no X to learn” when
they manufacture a contrast instead of correcting a likely misconception.
Preserve parallel headings and lists that distinguish real commands, formats,
process boundaries, or other technical categories.

- Avoid: “vfy now uses a new native-first execution architecture.”
- Prefer: “`vfy test` runs pytest-style tests and records their coverage.”

## Organize pages for their readers

Use [Ruff's tutorial](https://docs.astral.sh/ruff/tutorial/) as a model for
examples: establish the starting files, run a command, show the result, and
explain what to do next. Use [ty's documentation](https://docs.astral.sh/ty/)
as a model for separating guides, concepts, and reference. Follow these
structural patterns without copying prose or borrowing performance claims.

Prefer concrete names such as “saved coverage,” “passing baseline,” and
“selected tests” to “evidence” when they identify the actual data. Define terms
such as replay, passing baseline, and MC/DC before relying on them. Keep exact
schema names and diagnostic text where readers need to recognize them.

Put prerequisites before commands and expected results immediately after
them. State the working directory, files written, and expected nonzero exit
status when relevant. Label partial examples and placeholders. Keep baseline
and selected-run options consistent.

Use sentence case for headings and vfy for the product name; use `vfy`
for commands and imports. Preserve published anchors when changing headings.
Remove repeated explanations by linking to the page that owns the topic, but
keep a local warning when omitting it would make a command unsafe or misleading.

## Verify examples and claims

Show collection before reporting from `.coverage`. Keep coverage settings
consistent between baseline and selected runs. Verify public imports, constructor arguments, and option placement against the
implementation. Exercise changed workflows through `tests.support.vfy_project.VfyProject`.
Distinguish `vfy test` from direct `pytest --cov`, which uses pytest-cov when
installed. Describe pytest and xdist responsibilities accurately.

Qualify technical claims at their actual boundary:

- **Atomic:** identify the output being replaced and what happens on failure.
- **Deterministic:** identify the ordering or identity that is stable.
- **Compatible:** name the upstream version, interface, and tested behavior.
- **Faster:** summarize a measured benefit and name the comparison. In README
  and home-page prose, use readable, rounded figures and link to the benchmark
  page for workload, statistic, environment, samples, and parity checks. Keep
  commit IDs and exact timings in benchmark records. Verify mean versus median
  against retained results; link the raw data and reproduction command.

## Keep useful comments

Follow [Ruff's comment guidance](https://github.com/astral-sh/ruff/blob/main/AGENTS.md):
explain invariants and unusual choices in plain language. Keep an example when
it makes a subtle condition clear; brevity must not erase the reason.

Delete narration, repeated names, implementation praise, and unsupported claims.
Public Rust docs should state contracts, including errors and side effects;
private helpers need prose only when their names and types leave something
important unsaid. Do not restate “Rust owns” or “Python adapts” throughout the
code: explain the local constraint. Preserve safety and lint-suppression reasons,
executable fixtures, and runtime behavior.

- Avoid: `# Now loop through all of the shards and process them.`
- Prefer: `# Validate every shard before replacing the destination.`

Describe accepted data representations directly. Use “legacy” only when a real
external compatibility requirement needs that distinction. Diagnostics should
identify the problem and valid action, such as
`Unknown command 'report'; use vfy coverage report`.

## Review the result

Keep review inventories and agent reports out of the package. A prose cleanup
should remove redundancy, not add a record of the review.

Review at three levels: does the page answer its reader's question; does each
paragraph add a fact; does each sentence say it directly? Check claims against
code, examples against the public interface, and comments against the branch
they describe. Do not use a word-count target or vocabulary blacklist as proof
of quality.

Build with `uv run zensical build --clean --strict`, inspect affected rendered
pages, and check README links separately. Use the owning tests when examples or
diagnostics change; do not add tests that merely assert preferred wording.
For Markdown read on GitHub, check links between documentation pages as well as
README links. MkDocs `{#custom-id}` heading attributes do not create GitHub
anchors: use an explicit HTML anchor or a natural heading slug, and verify the
site still has no duplicate IDs.
