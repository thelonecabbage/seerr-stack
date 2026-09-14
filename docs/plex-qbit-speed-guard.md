# Plex qBittorrent Speed Guard

`scripts/plex_qbit_speed_guard.py` is intended to run on the Docker host. It reads
the existing Plex token and qBittorrent API key from local application config,
then uses only local application APIs.

The script:

- checks Plex `/status/sessions`;
- treats any non-stopped media session as active;
- enables qBittorrent alternative speed limits while Plex has active sessions;
- keeps alternative speed limits enabled during a short idle grace period;
- disables alternative speed limits after Plex stays idle;
- sets qBittorrent alternative download/upload limits when configured;
- avoids blind toggles by reading qBittorrent's current speed-limit mode first;
- writes a compact state file so idle grace survives separate cron runs;
- logs one summary line per run without printing tokens or API keys.

Default behavior:

- Plex URL: `http://127.0.0.1:32400`
- qBittorrent URL: `http://127.0.0.1:8081`
- idle grace: 120 seconds
- alternative download limit: 2 MiB/s
- alternative upload limit: unchanged

Install the script:

```sh
sudo install -m 0755 scripts/plex_qbit_speed_guard.py "$SEERR_STACK_REMOTE_PLEX_QBIT_SCRIPT"
```

Install the cron entry:

```sh
sudo install -m 0644 cron/plex-qbit-speed-guard "$SEERR_STACK_REMOTE_PLEX_QBIT_CRON"
```

Install the logrotate policy:

```sh
sudo install -m 0644 logrotate/plex-qbit-speed-guard "$SEERR_STACK_REMOTE_PLEX_QBIT_LOGROTATE"
```

Validate without changing qBittorrent:

```sh
sudo "$SEERR_STACK_REMOTE_PLEX_QBIT_SCRIPT" --dry-run
```

Validate the live current state:

```sh
sudo "$SEERR_STACK_REMOTE_PLEX_QBIT_SCRIPT"
```

Rollback:

```sh
sudo rm -f "$SEERR_STACK_REMOTE_PLEX_QBIT_CRON"
```

Then use qBittorrent's Web UI or API to choose the desired speed-limit mode.
