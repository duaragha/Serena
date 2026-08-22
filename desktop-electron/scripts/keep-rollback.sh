#!/usr/bin/env bash
# Keep the last known-good artifact so a bad update is recoverable.
#
# Serena IS the terminal Raghav works in, so an update that breaks the app also
# takes away the thing he would use to fix it. electron-updater has no
# downgrade path: once the AppImage is rewritten in place the previous build is
# gone. This copies the current artifact aside BEFORE a new one is installed,
# and prints the exact command to go back.
#
# Usage:
#   scripts/keep-rollback.sh            # archive the current build
#   scripts/keep-rollback.sh --list     # show what can be rolled back to
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ARCHIVE="${SERENA_ROLLBACK_DIR:-$HOME/.local/share/serena/rollback}"
KEEP="${SERENA_ROLLBACK_KEEP:-3}"

mkdir -p "$ARCHIVE"

if [[ "${1:-}" == "--list" ]]; then
  if compgen -G "$ARCHIVE/*" >/dev/null; then
    ls -1t "$ARCHIVE"
    echo
    echo "Roll back by running one directly, or reinstall it:"
    echo "  cp $ARCHIVE/<file> $ROOT/dist/ && chmod +x $ROOT/dist/<file>"
  else
    echo "No archived builds yet."
  fi
  exit 0
fi

archived=0
for artifact in \
  "$ROOT/dist/Serena-"*.AppImage \
  "$ROOT/dist/windows/Serena-Setup-"*.exe
do
  [[ -f "$artifact" ]] || continue
  name="$(basename "$artifact")"
  stamp="$(date -r "$artifact" +%Y%m%d-%H%M%S)"
  target="$ARCHIVE/${stamp}-${name}"
  if [[ ! -f "$target" ]]; then
    cp -p "$artifact" "$target"
    echo "archived $name -> $target"
    archived=$((archived + 1))
  fi
done

if [[ "$archived" -eq 0 ]]; then
  echo "nothing new to archive"
fi

# Keep the archive bounded; these are hundreds of MB each.
mapfile -t stale < <(ls -1t "$ARCHIVE" 2>/dev/null | tail -n +$((KEEP + 1)))
for old in "${stale[@]:-}"; do
  [[ -n "$old" ]] || continue
  rm -f "$ARCHIVE/$old"
  echo "pruned $old"
done
