#!/usr/bin/env bash
# Copies the shared workflow files from the backend repo (canonical) to the web repo.
set -euo pipefail
SRC="$(cd "$(dirname "$0")/.." && pwd)"
DST="${1:-$SRC/../../ReactProjects/stock_pilot_ai_web_app}"
mkdir -p "$DST/.claude/agents" "$DST/docs/workflow"
cp "$SRC"/.claude/agents/*.md "$DST/.claude/agents/"
cp "$SRC/docs/workflow/WORKFLOW.md" "$SRC/docs/workflow/CONTRACT.md" "$DST/docs/workflow/"
cp "$SRC/docs/SPEC.md" "$DST/docs/SPEC.md"
echo "synced to $DST"
