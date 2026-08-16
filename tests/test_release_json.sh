#!/usr/bin/env bash
# Schema and artifact checks for offline/release.json, without network access.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RELEASE_META="$REPO_ROOT/offline/release_meta.py"
FAILED=0

pass() { printf 'ok   %s\n' "$1"; }
fail() { printf 'FAIL %s\n' "$1"; FAILED=1; }
expect_ok() {
  local name="$1"; shift
  if "$@" > "$TMP/stdout" 2> "$TMP/stderr"; then pass "$name"; else
    fail "$name"; sed 's/^/       /' "$TMP/stderr"
  fi
}
expect_fail() {
  local name="$1" pattern="$2"; shift 2
  if "$@" > "$TMP/stdout" 2> "$TMP/stderr"; then
    fail "$name (unexpected exit 0)"
  elif grep -q "$pattern" "$TMP/stderr"; then
    pass "$name"
  else
    fail "$name (missing diagnostic: $pattern)"; sed 's/^/       /' "$TMP/stderr"
  fi
}

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

BUNDLE="$TMP/gpu-triage-bundle-2099.01.01.zip"
printf 'synthetic bundle\n' > "$BUNDLE"
BUNDLE_SHA="$(sha256sum "$BUNDLE" | awk '{print $1}')"
BUNDLE_SIZE="$(stat -c %s "$BUNDLE")"

write_release() {
  local path="$1" schema="${2:-1}"
  cat > "$path" <<EOF
{
  "schema": $schema,
  "generated": "2099-01-01T00:00:00Z",
  "iso_date": "2099.01.01",
  "iso_name": "archlinux-2099.01.01-x86_64.iso",
  "iso_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "iso_size": 1024,
  "iso_urls": [
    "https://geo.mirror.pkgbuild.com/iso/2099.01.01/archlinux-2099.01.01-x86_64.iso",
    "https://archive.archlinux.org/iso/2099.01.01/archlinux-2099.01.01-x86_64.iso"
  ],
  "expected_kernel": "9.9.9-arch1-1",
  "release_tag": "bundle-2099.01.01",
  "bundle_name": "gpu-triage-bundle-2099.01.01.zip",
  "bundle_url": "https://github.com/example/gpu-triage/releases/download/bundle-2099.01.01/gpu-triage-bundle-2099.01.01.zip",
  "bundle_sha256": "$BUNDLE_SHA",
  "bundle_size": $BUNDLE_SIZE,
  "bundle_packages": 2
}
EOF
}

write_release "$TMP/valid.json"
expect_ok 'complete schema-1 release is accepted' \
  python3 "$RELEASE_META" check --release "$TMP/valid.json"
expect_ok 'bundle name, size and sha256 are verified' \
  python3 "$RELEASE_META" check --release "$TMP/valid.json" --bundle "$BUNDLE"

python3 - "$TMP/valid.json" "$TMP/missing.json" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
del data["expected_kernel"]
json.dump(data, open(sys.argv[2], "w", encoding="utf-8"))
PY
expect_fail 'missing required field is rejected' 'missing field: expected_kernel' \
  python3 "$RELEASE_META" check --release "$TMP/missing.json"

write_release "$TMP/future.json" 2
expect_fail 'unknown schema is rejected' 'schema 2, expected 1' \
  python3 "$RELEASE_META" check --release "$TMP/future.json"

python3 - "$TMP/valid.json" "$TMP/no-archive.json" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
data["iso_urls"] = data["iso_urls"][:1]
json.dump(data, open(sys.argv[2], "w", encoding="utf-8"))
PY
expect_fail 'durable archive ISO fallback is required' 'no archive.archlinux.org fallback' \
  python3 "$RELEASE_META" check --release "$TMP/no-archive.json"

printf 'changed bundle\n' > "$BUNDLE"
expect_fail 'changed release asset is rejected' 'bundle_size\|bundle_sha256' \
  python3 "$RELEASE_META" check --release "$TMP/valid.json" --bundle "$BUNDLE"

printf '{not json\n' > "$TMP/malformed.json"
expect_fail 'malformed JSON is rejected' 'not valid JSON' \
  python3 "$RELEASE_META" check --release "$TMP/malformed.json"

if [[ "$FAILED" -eq 0 ]]; then
  echo 'release.json tests: PASS'
else
  echo 'release.json tests: FAIL'
fi
exit "$FAILED"
