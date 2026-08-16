#!/usr/bin/env bash

# Pure helpers shared by build_bundle.sh and its synthetic regression tests.
# The caller deliberately owns logging and file lifecycle; these functions only
# parse the two package lists and derive filesystem-safe package names.

bundle_load_iso_packages() {
  local package_list="$1" map_name="$2"
  local -n packages="$map_name"
  local name version _

  packages=()
  BUNDLE_ISO_PKG_COUNT=0
  while read -r name version _; do
    [[ -n "$name" && -n "$version" ]] || continue
    packages["$name"]="$version"
    BUNDLE_ISO_PKG_COUNT=$((BUNDLE_ISO_PKG_COUNT + 1))
  done < "$package_list"
}

bundle_split_closure() {
  local closure="$1" map_name="$2" subtract="$3" keep="$4" excluded="$5"
  local -n iso_packages="$map_name"
  local repo name version location file

  : > "$keep"
  : > "$excluded"
  BUNDLE_DRIFT=0

  while read -r repo name version location; do
    [[ -n "$name" ]] || continue
    file="${location##*/}"
    if [[ "$subtract" -eq 1 && -n "${iso_packages[$name]:-}" ]]; then
      if [[ "${iso_packages[$name]}" == "$version" ]]; then
        printf '%s %s\n' "$name" "$version" >> "$excluded"
        continue
      fi
      # A version mismatch is safe to ship and unsafe to omit. Report it so a
      # stale ISO/snapshot pairing remains visible to the builder.
      BUNDLE_DRIFT=$((BUNDLE_DRIFT + 1))
    fi
    printf '%s\t%s\t%s\t%s\n' "$repo" "$name" "$version" "$file" >> "$keep"
  done < "$closure"

  sort -o "$excluded" "$excluded"
  BUNDLE_EXCLUDED_COUNT="$(wc -l < "$excluded")"
  BUNDLE_KEEP_COUNT="$(wc -l < "$keep")"

  if [[ "$BUNDLE_DRIFT" -gt 0 ]]; then
    printf '[bundle] WARNING: %s package(s) exist on the ISO at a different version and are shipped anyway.\n' \
      "$BUNDLE_DRIFT" >&2
    printf '[bundle] WARNING: A large number here means the ISO date and the archive snapshot do not line up.\n' >&2
  fi
}

bundle_windows_filename() {
  printf '%s\n' "${1//:/_}"
}
