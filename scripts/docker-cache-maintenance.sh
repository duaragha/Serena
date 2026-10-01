#!/usr/bin/env bash
# Installed only inside the gaming PC's Docker-Ubuntu VM.
set -Eeuo pipefail

mode=${1:---dry-run}
case "$mode" in
  --dry-run|--apply) ;;
  *) echo 'usage: docker-cache-maintenance.sh [--dry-run|--apply]' >&2; exit 2 ;;
esac

[[ $(hostname -s) == docker-vm ]] || {
  echo 'refusing cache maintenance outside docker-vm' >&2
  exit 1
}
unset DOCKER_HOST DOCKER_CONTEXT DOCKER_TLS_VERIFY DOCKER_CERT_PATH BUILDX_BUILDER
docker_cmd=(docker --context default)
[[ $("${docker_cmd[@]}" info --format '{{.Name}}') == docker-vm ]] || {
  echo 'default Docker context is not the expected docker-vm daemon' >&2
  exit 1
}

history=$("${docker_cmd[@]}" buildx --builder default history ls --format json)
active_build=$(jq -s 'if all(.[]; (.status | type) == "string") then
  any(.[]; (.status | ascii_downcase) == "running")
  else error("invalid build history") end' <<<"$history")
if [[ $active_build == true ]]; then
  echo 'build running; cache maintenance deferred until the next timer run'
  exit 0
fi

commands=(
  'remove unused cache last accessed more than 7 days ago'
  'target 40,000,000,000 cache bytes; preserve entries used in the last 24 hours'
)
printf '%s\n' "${commands[@]}"
if [[ $mode == --dry-run ]]; then
  "${docker_cmd[@]}" buildx --builder default du | tail -n 4
  exit 0
fi

exec 9>"${RUNTIME_DIRECTORY:-/run/serena-docker-cache}/prune.lock"
flock -n 9 || { echo 'another cache maintenance invocation is running'; exit 0; }
"${docker_cmd[@]}" buildx --builder default prune --all --force --filter until=168h
"${docker_cmd[@]}" buildx --builder default prune --all --force --filter until=24h \
  --max-used-space 40000000000 --reserved-space 15000000000
"${docker_cmd[@]}" buildx --builder default du | tail -n 4
df -B1 /
