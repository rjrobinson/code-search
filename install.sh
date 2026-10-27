#!/usr/bin/env bash
# Install code-search for Claude Code: Python env, user-scope MCP server, Ruby check. Safe to run again.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
PY="$REPO/.venv/bin/python"

need() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "error: $1 not found. $2" >&2
    exit 1
  fi
}
need git "Install git first."
need uv "Install it: https://docs.astral.sh/uv/getting-started/installation/"
need claude "Install the Claude Code CLI: https://code.claude.com/docs/en/setup"

echo "==> Installing Python dependencies (uv sync)"
(cd "$REPO" && uv sync --locked)

echo
echo "==> Registering the code-search MCP server (user scope)"
if existing="$(claude mcp get code-search 2>/dev/null)"; then
  if grep -qF "$REPO/code_search.py" <<<"$existing"; then
    echo "Already registered to this checkout. Nothing to do."
  else
    echo "warning: a code-search MCP server is already registered, but not to this checkout:" >&2
    grep -E 'Scope:|Command:|Args:' <<<"$existing" >&2 || true
    echo "To use this checkout instead, run: claude mcp remove code-search -s user" >&2
    echo "Then run this script again." >&2
  fi
else
  claude mcp add --scope user code-search -- "$PY" "$REPO/code_search.py" serve
fi

echo
echo "==> Checking for a Ruby with Prism (Ruby 3.3+)"
ruby="$("$PY" -B -c 'import sys; sys.path.insert(0, sys.argv[1]); import code_search; print(code_search.ruby_bin() or "")' "$REPO")"
if [[ -n "$ruby" ]]; then
  echo "Found: $ruby"
  echo "Ruby files get one chunk per method."
else
  echo "Not found. Ruby files will get plain line-window chunks, which search less well."
  echo "To fix: install Ruby 3.3+ (asdf, rbenv, mise or Homebrew), or set CODE_SEARCH_RUBY to a Ruby 3.3+ binary."
  echo "Repos without Ruby are not affected."
fi

cat <<EOF

==> Optional: skip the permission prompts
Add these two rules to "permissions": { "allow": [...] } in ~/.claude/settings.json:

    "mcp__code-search__code_search",
    "mcp__code-search__code_index"

This script does not edit your settings.

==> Build your first index
Restart any open Claude Code sessions so they load the server. Then, from inside a repo, either ask Claude
to "build the code_search index" (it calls code_index and builds in the background), or run:

    $PY $REPO/code_search.py index

The first build downloads the embedding model (about 1.2 GB) once. Build time grows with repo size:
about 27 min for a large repo (60k chunks) on an Apple Silicon GPU, under a minute for a tiny one.
EOF
