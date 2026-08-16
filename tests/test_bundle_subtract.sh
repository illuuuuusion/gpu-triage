#!/usr/bin/env bash
# Synthetic regression tests for ISO subtraction and Windows-safe filenames.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$REPO_ROOT/offline/bundle_helpers.sh"

FAILED=0
pass() { printf 'ok   %s\n' "$1"; }
fail() { printf 'FAIL %s\n' "$1"; FAILED=1; }
check() {
  if [[ "$2" == "$3" ]]; then pass "$1"; else
    printf 'FAIL %s\n       expected: %s\n       actual:   %s\n' "$1" "$3" "$2"
    FAILED=1
  fi
}

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

cat > "$TMP/isopkgs.txt" <<'EOF'
exact 1.0-1
drift 1.0-1
linux 7.1.5.arch1-2
EOF

cat > "$TMP/closure.txt" <<'EOF'
core exact 1.0-1 https://archive.invalid/exact-1.0-1-x86_64.pkg.tar.zst
extra drift 2.0-1 https://archive.invalid/drift-2.0-1-x86_64.pkg.tar.zst
extra fresh 3.0-1 https://archive.invalid/fresh-3.0-1-x86_64.pkg.tar.zst
extra epoch 1:4.0-1 https://archive.invalid/epoch-1:4.0-1-x86_64.pkg.tar.zst
EOF

declare -A ISO_PKGS=()
bundle_load_iso_packages "$TMP/isopkgs.txt" ISO_PKGS
check 'ISO package count is parsed' "$BUNDLE_ISO_PKG_COUNT" '3'
check 'ISO package version is retained' "${ISO_PKGS[drift]}" '1.0-1'

bundle_split_closure "$TMP/closure.txt" ISO_PKGS 1 \
  "$TMP/keep.txt" "$TMP/excluded.txt" 2> "$TMP/warnings.txt"

check 'exact name+version match is excluded' "$(cat "$TMP/excluded.txt")" 'exact 1.0-1'
check 'only the exact match is excluded' "$BUNDLE_EXCLUDED_COUNT" '1'
check 'version drift and absent packages are shipped' "$BUNDLE_KEEP_COUNT" '3'
grep -q $'extra\tdrift\t2.0-1\tdrift-2.0-1-x86_64.pkg.tar.zst' "$TMP/keep.txt" \
  && pass 'version mismatch remains in bundle' || fail 'version mismatch remains in bundle'
grep -q $'extra\tfresh\t3.0-1\tfresh-3.0-1-x86_64.pkg.tar.zst' "$TMP/keep.txt" \
  && pass 'package absent from ISO remains in bundle' || fail 'package absent from ISO remains in bundle'
check 'version drift count is exposed' "$BUNDLE_DRIFT" '1'
grep -q 'WARNING: 1 package(s).*different version.*shipped anyway' "$TMP/warnings.txt" \
  && pass 'version drift is reported clearly' || fail 'version drift is reported clearly'

bundle_split_closure "$TMP/closure.txt" ISO_PKGS 0 \
  "$TMP/full.txt" "$TMP/full-excluded.txt" 2> "$TMP/full-warnings.txt"
check '--no-iso-subtract keeps the full closure' "$BUNDLE_KEEP_COUNT" '4'
check '--no-iso-subtract excludes nothing' "$BUNDLE_EXCLUDED_COUNT" '0'
check '--no-iso-subtract reports no drift' "$BUNDLE_DRIFT" '0'
[[ ! -s "$TMP/full-warnings.txt" ]] \
  && pass '--no-iso-subtract emits no drift warning' || fail '--no-iso-subtract emits no drift warning'

check 'epoch colon is made Windows-safe' \
  "$(bundle_windows_filename 'mesa-1:26.1.6-1-x86_64.pkg.tar.zst')" \
  'mesa-1_26.1.6-1-x86_64.pkg.tar.zst'
check 'ordinary filename is unchanged' \
  "$(bundle_windows_filename 'python-3.13.5-1-x86_64.pkg.tar.zst')" \
  'python-3.13.5-1-x86_64.pkg.tar.zst'

if [[ "$FAILED" -eq 0 ]]; then
  echo 'bundle subtraction tests: PASS'
else
  echo 'bundle subtraction tests: FAIL'
fi
exit "$FAILED"
