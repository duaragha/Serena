# Source before starting native build tools from an AppImage-hosted terminal.
if [[ -v LD_LIBRARY_PATH_ORIG ]]; then
  LD_LIBRARY_PATH="$LD_LIBRARY_PATH_ORIG"
fi
if [[ -n "${APPDIR:-}" ]]; then
  IFS=: read -r -a serena_library_paths <<< "${LD_LIBRARY_PATH:-}"
  serena_native_paths=()
  for serena_path in "${serena_library_paths[@]}"; do
    if [[ -n "$serena_path" && "$serena_path" != "$APPDIR" && "$serena_path" != "$APPDIR/"* ]]; then
      serena_native_paths+=("$serena_path")
    fi
  done
  LD_LIBRARY_PATH="$(IFS=:; printf '%s' "${serena_native_paths[*]}")"
  unset serena_library_paths serena_native_paths serena_path
fi
if [[ -n "${LD_LIBRARY_PATH:-}" ]]; then
  export LD_LIBRARY_PATH
else
  unset LD_LIBRARY_PATH
fi
unset LD_LIBRARY_PATH_ORIG APPDIR APPIMAGE
