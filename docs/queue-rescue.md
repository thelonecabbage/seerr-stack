# Queue Rescue

`scripts/seerr_queue_rescue.py` is intended to run on the Docker host. It reads local Sonarr, Radarr, and qBittorrent runtime API keys from their existing config files, then uses only loopback application APIs.

The script:

- restores healthy qBittorrent queue, connection, and speed-limit settings;
- disables qBittorrent alternate speed-limit mode if it is active;
- keeps a wider active-download window so queued items can try peers sooner;
- promotes recent Sonarr/Radarr releases to top qBittorrent priority;
- detects stalled torrent queue groups;
- removes failed/stale downloads from the client;
- blocklists failed releases in Sonarr/Radarr;
- triggers a fresh search for the affected episode or movie;
- treats unsafe executable payloads as immediate failures.

Defaults are conservative and can be overridden in the cron file:

- Sonarr recent release window: 336 hours
- Sonarr recent retry threshold: 2 hours
- Sonarr old backlog threshold: 48 hours
- Radarr recent release window: 720 hours
- Radarr recent retry threshold: 4 hours
- Radarr old backlog threshold: 48 hours

The script uses actual Sonarr air dates and Radarr release dates for recent-release prioritization, not the time the item was added to the downloader.
