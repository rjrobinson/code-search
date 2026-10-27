#!/usr/bin/env bash
# Remove code-search's MCP registration and Python env. Indexes and the model cache stay until you delete them.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"

echo "==> Removing the code-search MCP server"
if ! command -v claude >/dev/null 2>&1; then
  echo "claude not found. Skipping."
elif existing="$(claude mcp get code-search 2>/dev/null)"; then
  if grep -qF "$REPO/code_search.py" <<<"$existing"; then
    if ! claude mcp remove code-search -s user; then
      echo "warning: removal failed. Check the scope with: claude mcp get code-search" >&2
    fi
  else
    echo "code-search is registered to a different checkout. Leaving it alone:"
    grep -E 'Scope:|Command:|Args:' <<<"$existing" || true
  fi
else
  echo "Not registered. Nothing to do."
fi

echo
echo "==> Removing the Python env"
rm -rf "$REPO/.venv"
echo "Removed $REPO/.venv"

index_root="${CODE_SEARCH_HOME:-$HOME/.leann/repos}"
usage_log="${CODE_SEARCH_USAGE_LOG:-$HOME/.leann/usage.jsonl}"
model_cache="${HF_HOME:-$HOME/.cache/huggingface}/hub/models--Qwen--Qwen3-Embedding-0.6B"
cat <<EOF

==> Data left in place
Restart open Claude Code sessions to stop their search servers. To free the disk space, delete:

    rm -rf "$index_root"     # indexes, about 400 MB to 800 MB per large repo
    rm -f "$usage_log"       # local usage log
    rm -rf "$model_cache"    # embedding model, about 1.2 GB

Then delete this checkout.
EOF
