# Seerr Stack

Sanitized Portainer stack source for Seerr, qBittorrent, Prowlarr, Sonarr, Radarr, SABnzbd, and host-side automation.

This repository is intended to store deployable stack code and scripts only. Runtime application config, databases, API keys, cookies, provider credentials, and downloader state must stay outside Git.

## Contents

- `docker-compose.yml`: Portainer-compatible Compose file using environment placeholders.
- `.env.example`: host-specific values to copy into an untracked `.env`.
- `scripts/seerr_queue_rescue.py`: qBittorrent/Sonarr/Radarr queue rescue automation.
- `scripts/plex_qbit_speed_guard.py`: Plex-aware qBittorrent alternative-speed guard.
- `cron/seerr-queue-rescue`: cron entry that runs the rescue script every 30 minutes on the Docker host.
- `cron/plex-qbit-speed-guard`: cron entry that runs the Plex/qBittorrent speed guard every minute.
- `logrotate/plex-qbit-speed-guard`: log rotation policy for the speed guard.

## Deploy

1. Copy `.env.example` to `.env` on the Docker host.
2. Review host paths and ports in `.env`.
3. Deploy `docker-compose.yml` from Portainer or Docker Compose.
4. Install the rescue script:

   ```sh
   sudo install -m 0755 scripts/seerr_queue_rescue.py "$SEERR_RESCUE_SCRIPT"
   ```

5. Install the cron file:

   ```sh
   sudo install -m 0644 cron/seerr-queue-rescue "$SEERR_STACK_REMOTE_CRON"
   ```

6. Install the Plex/qBittorrent speed guard:

   ```sh
   sudo install -m 0755 scripts/plex_qbit_speed_guard.py "$SEERR_STACK_REMOTE_PLEX_QBIT_SCRIPT"
   sudo install -m 0644 cron/plex-qbit-speed-guard "$SEERR_STACK_REMOTE_PLEX_QBIT_CRON"
   sudo install -m 0644 logrotate/plex-qbit-speed-guard "$SEERR_STACK_REMOTE_PLEX_QBIT_LOGROTATE"
   ```

See `docs/plex-qbit-speed-guard.md` for behavior, validation, and rollback.

## Secret Rules

Do not commit:

- `.env`
- runtime config directories
- qBittorrent config files
- Sonarr/Radarr/Prowlarr config XML files
- Seerr/Jellyseerr databases or app config
- SABnzbd config
- indexer, provider, proxy, cookie, or API credentials
