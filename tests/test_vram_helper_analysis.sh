#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

cc -std=c17 -Wall -Wextra -Wpedantic -Werror \
  -I"$REPO_ROOT/vram-helper" \
  "$REPO_ROOT/vram-helper/analysis.c" \
  "$REPO_ROOT/vram-helper/analysis_test.c" \
  -o "$TMP/analysis-test"
"$TMP/analysis-test"

grep -q 'pattern == 0' "$REPO_ROOT/vram-helper/shaders/pattern.comp"
grep -q 'pattern == 8' "$REPO_ROOT/vram-helper/shaders/pattern.comp"
grep -q '1664525u' "$REPO_ROOT/vram-helper/shaders/pattern.comp"
