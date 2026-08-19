#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
desktop_dir="$(cd "$script_dir/.." && pwd)"

rm -rf \
  "$desktop_dir/build/pyinstaller-work" \
  "$desktop_dir/build/sidecar" \
  "$desktop_dir/build/uv-cache" \
  "$desktop_dir/build/uv-tools" \
  "$desktop_dir/build/pyinstaller-tool" \
  "$desktop_dir/build/pyinstaller-config" \
  "$desktop_dir/dist/linux-unpacked" \
  "$desktop_dir/dist/__appImage-x64" \
  "$desktop_dir/dist/builder-debug.yml" \
  "$desktop_dir/dist/.icon-icns" \
  "$desktop_dir/dist/.icon-ico" \
  "$desktop_dir/__pycache__" \
  "$desktop_dir/tests/__pycache__"
