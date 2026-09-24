# Queue Rescue

`scripts/seerr_queue_rescue.py` is intended to run on the Docker host. It reads local Sonarr, Radarr, and qBittorrent runtime API keys from their existing config files, then uses only loopback application APIs.

The script:

- restores healthy qBittorrent queue, connection, and speed-limit settings;
- disables qBittorrent alternate speed-limit mode if it is active;
- keeps a wider active-download window so queued items can try peers sooner;
- promotes recent Sonarr/Radarr releases to top qBittorrent priority;
- persists progress observations and detects stalled torrent queue groups;
- removes failed/stale downloads from the client;
- blocklists failed releases in Sonarr/Radarr;
- submits exactly one fresh search for the affected episodes or movies per rescue;
- retries completed imports only after verifying their files and storage;
- clears fully verified imported queue groups without deleting any files;
- retains executable payloads for review and never submits them for import.

Defaults are conservative and can be overridden in the cron file:

- Sonarr recent release window: 336 hours
- Sonarr recent retry threshold: 2 hours
- Sonarr old backlog threshold: 48 hours
- Radarr recent release window: 720 hours
- Radarr recent retry threshold: 4 hours
- Radarr old backlog threshold: 48 hours

The script uses actual Sonarr air dates and Radarr release dates for recent-release prioritization, not the time the item was added to the downloader.

## Observed Stalls

The cron runs every 30 minutes. Metadata downloads with no connected peers are
eligible after two observed hours. Zero-progress downloads with no seeds and zero
availability are eligible after four observed hours. Other stalled downloads keep
the recent/backlog thresholds above, measured from the beginning of the observed
stall rather than their age in the queue.

Increasing downloaded bytes, progress, activity timestamps, or a positive current
download speed reset the stall timer. Paused, stopped, queued, checking, moving,
errored, and completed torrents are excluded. Tracker errors and missing ETA alone
do not qualify. Unknown or negative availability is not treated as zero.

A restart resets observations and adds a 30-minute grace period. An observation
gap longer than 75 minutes also resets the timers. Consequently, the first install
waits for fresh evidence; it does not immediately replace old queue entries.

Each run can rescue at most five Sonarr groups and three Radarr groups. Each media
ID is limited to two replacements in a rolling 24 hours, even when the torrent hash
changes. Season packs are grouped by hash and every represented episode must be
eligible. qBittorrent is rechecked immediately before replacement.

The queue removal uses `skipRedownload=true`, followed by a single explicit search.
The script reserves an attempt on disk before making those calls. If either call
has an ambiguous result, the same hash is left for review rather than automatically
replaying a deletion or search. Completed media is never sent through this path.

## Completed Imports

Import recovery waits one hour from its first observation and skips an import
already running. It requires readable, nonempty selected video files, a matching
host/container bind mount backed by a non-root host filesystem, and a writable
destination checked inside the application container as its configured PUID/PGID.
Symlinks must stay within the verified mount. Deployments storing media on the host
root filesystem deliberately require review instead of automatic import recovery.

Retries use a path-scoped `DownloadedMoviesScan` or `DownloadedEpisodesScan` with
`importMode=Copy`, preserving source files. There are at most two import/cleanup
actions per application per run, two import retries per hash, and six hours between
retries. Missing files, invalid payloads, permissions, and mount errors produce
review messages without blocklisting or searching for another release.

Cleanup requires matching import history for every media ID in the queue group,
matching current library file IDs and paths, readable nonempty library files, and
import records for all selected torrent video files. A partially imported season
pack is retained. Verified cleanup detaches the torrent with `deleteFiles=false`
and clears the queue with client removal, blocklisting, and redownload disabled.

## Operation

State defaults to `/var/lib/seerr-queue-rescue/state.json`, outside the repository.
Writes are atomic and private; a file lock prevents overlapping runs. A corrupt
state file stops the run. Retain the state when updating the script so cooldowns
and ambiguous-operation reservations survive deployment.

`--dry-run` makes no application writes and does not save observations or attempts.
It may create the state directory and lock file. `--observe` persists observations
and import grace timestamps but makes no application writes. With both options,
`--dry-run` takes precedence. Logs include UTC timestamps, reasons, thresholds,
cooldowns, submitted command IDs, and run summaries. Port-mapping failures are
diagnostic only; the script does not alter router configuration.

Additional cron settings:

| Variable | Default |
| --- | --- |
| `SEERR_RESCUE_METADATA_HOURS` | 2 |
| `SEERR_RESCUE_ZERO_AVAIL_HOURS` | 4 |
| `SEERR_RESCUE_RESTART_GRACE_MINUTES` | 30 |
| `SEERR_RESCUE_MAX_REPLACEMENTS_24H` | 2 |
| `SEERR_RESCUE_STATE_FILE` | `/var/lib/seerr-queue-rescue/state.json` |
| `SEERR_RESCUE_IMPORT_ENABLED` | 0 in script, 1 in installed cron |
| `SEERR_RESCUE_CLEANUP_ENABLED` | 0 in script, 1 in installed cron |
| `SEERR_RESCUE_MAX_IMPORT_ACTIONS` | 2 per application |
| `SEERR_RESCUE_MAX_IMPORT_RETRIES` | 2 per hash |
| `SEERR_RESCUE_IMPORT_GRACE_MINUTES` | 60 |

Container names default to `qbittorrent`, `sonarr`, and `radarr`; override through
`SEERR_RESCUE_QBITTORRENT_CONTAINER`, `SEERR_RESCUE_SONARR_CONTAINER`, or
`SEERR_RESCUE_RADARR_CONTAINER`. The existing config directory setting remains
deployment-specific.

Before deploying, run `python3 -m unittest discover -s tests -v`, back up the live
script and cron outside the cron directory, and stage an `--observe` run. Inspect
the decisions and container filesystem checks before enabling import/cleanup.
After installing, execute with the same environment as cron and inspect the log.
To roll back, restore the backed-up script and cron; keep the state for diagnosis.
