#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
desktop_dir="$(cd "$script_dir/.." && pwd)"
repo_root="$(cd "$desktop_dir/.." && pwd)"
python_bin="${SERENA_PYTHON:-$repo_root/.venv/bin/python}"
uv_bin="${UV_BIN:-$(command -v uv || true)}"
pyinstaller_work="$desktop_dir/build/pyinstaller-work"
sidecar_dist="$desktop_dir/build/sidecar"
uv_cache="$desktop_dir/build/uv-cache"
uv_tools="$desktop_dir/build/uv-tools"

if [[ ! -x "$python_bin" ]]; then
  echo "repo venv Python not found at $python_bin" >&2
  exit 1
fi
if ! "$python_bin" -c 'import PyInstaller' 2>/dev/null; then
  echo "PyInstaller is not installed in the repo venv: $python_bin -m pip install pyinstaller" >&2
  exit 1
fi

site_packages="$($python_bin -c 'import site; print(site.getsitepackages()[0])')"
rm -rf "$pyinstaller_work" "$sidecar_dist"
mkdir -p "$pyinstaller_work" "$sidecar_dist" "$uv_cache" "$uv_tools"

# PyInstaller MUST run from the same interpreter as the app. The previous
# uv-tool approach analyzed under uv's own Python (3.13) against the 3.12 venv,
# which bundled numpy with broken/missing compiled extensions (numpy/_core).
"$python_bin" -m PyInstaller \
  --noconfirm \
  --clean \
  --onedir \
  --name serena-web-sidecar \
  --distpath "$sidecar_dist" \
  --workpath "$pyinstaller_work" \
  --specpath "$pyinstaller_work" \
  --paths "$repo_root" \
  --paths "$site_packages" \
  --collect-all numpy \
  --add-data "$repo_root/ui/static:ui/static" \
  "$desktop_dir/sidecar.py"

test -x "$sidecar_dist/serena-web-sidecar/serena-web-sidecar"
