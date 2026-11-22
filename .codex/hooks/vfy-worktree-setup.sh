#!/bin/bash
set -euo pipefail

session_cwd=$(jq -r '.cwd // empty')
[[ -n "$session_cwd" ]] || exit 0
cd "$session_cwd"
repo_root=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
cd "$repo_root"

git_dir=$(git rev-parse --path-format=absolute --git-dir)
common_dir=$(git rev-parse --path-format=absolute --git-common-dir)
[[ "$git_dir" != "$common_dir" ]] || exit 0
[[ ! -e .venv ]] || exit 0

# Keep this environment inside the worktree even if the parent session overrides uv.
export UV_PROJECT_ENVIRONMENT="$repo_root/.venv"
uv sync --locked --group dev >&2
uv run maturin develop --release --skip-install >&2
