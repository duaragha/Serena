# Gaming PC build cache maintenance

The Docker-Ubuntu VM runs Docker Engine's default BuildKit builder. Install
`scripts/docker-cache-maintenance.sh` at `/usr/local/bin/` (mode 0755), and
`systemd/pc/docker-cache-maintenance.{service,timer}` at `/etc/systemd/system/`
(mode 0644). These are system units inside the VM, not laptop user units.

Reload systemd with `sudo systemctl daemon-reload`, inspect the script using
`sudo -u dockeradmin /usr/local/bin/docker-cache-maintenance.sh --dry-run`, then
enable the timer with `sudo systemctl enable --now docker-cache-maintenance.timer`.
Enabling a persistent timer can trigger overdue maintenance immediately.
Docker and the VM do not need to restart.

The timer runs daily at 04:45 America/Toronto plus up to 15 minutes of jitter.
The script checks the local host and daemon name, skips when a build is running,
and serializes concurrent invocations with `flock`. BuildKit itself protects
records used by a build that starts after the history check.

Two passes delete unused build cache only:

1. Cache last accessed more than seven days ago is removed.
2. Any unused cache is eligible for a 40 GB target (40,000,000,000 bytes), with
   15 GB reserved for reusable cache. BuildKit prioritizes eviction using cache
   recency and frequency. This pass has no age filter: releasing an old child
   layer can make its newly eligible parent appear recently used, preventing
   an age-restricted pass from reaching the budget.

This is a target, not an absolute quota: active builds, dependencies
and data shared with images can keep reported use above it. `--all` includes
BuildKit internal/frontend references; it does not delete Docker images,
containers, networks or volumes. Existing image tags remain available for rollback.
Evicted cache is recreated on demand; an affected build can take longer.

Check `systemctl list-timers docker-cache-maintenance.timer`,
`journalctl -u docker-cache-maintenance.service`, and `docker buildx du` on the VM.
Disable with `sudo systemctl disable --now docker-cache-maintenance.timer`.
The script defaults to `--dry-run` and requires `--apply` to remove cache.

Sources: [Buildx prune](https://docs.docker.com/reference/cli/docker/buildx/prune/),
[cache optimization](https://docs.docker.com/build/cache/optimize/).
