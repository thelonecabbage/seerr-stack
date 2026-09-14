# Focus Oversized Radarr Imports In Unmanic

`scripts/focus_oversized_radarr_unmanic.py` is intended to run on the Docker
host. It tracks oversized Radarr movie downloads and only prioritizes them in
Unmanic after Radarr has imported the final movie file.

The script:

- discovers Radarr queue items at or above the configured byte threshold;
- tracks those items by download ID in a local state file;
- waits while the download is still visible in Radarr's queue;
- resolves the movie through Radarr history and the movie file record;
- maps Radarr's movie path into the path visible to Unmanic;
- requires the final file to exist, be above the threshold, and be old enough;
- checks that size and mtime remain unchanged before enqueueing;
- inserts or reprioritizes only that exact file in Unmanic's pending task table;
- logs sanitized titles, paths, and status without printing API keys or source URLs.

Default behavior:

- oversized threshold: 40 GiB
- minimum final-file age: 30 minutes
- stability check: 20 seconds
- Radarr URL: `http://127.0.0.1:7878/api/v3`
- Radarr movie prefix: `/movies/`
- Unmanic movie prefix: `/media/movies/`
- Unmanic library ID: `1`

Install the script:

```sh
sudo install -m 0755 scripts/focus_oversized_radarr_unmanic.py "$SEERR_STACK_REMOTE_OVERSIZED_UNMANIC_SCRIPT"
```

Install the cron entry:

```sh
sudo install -m 0644 cron/focus-oversized-radarr-unmanic "$SEERR_STACK_REMOTE_OVERSIZED_UNMANIC_CRON"
```

Validate without enqueueing anything:

```sh
sudo "$SEERR_STACK_REMOTE_OVERSIZED_UNMANIC_SCRIPT" --dry-run
```

Run one live pass:

```sh
sudo "$SEERR_STACK_REMOTE_OVERSIZED_UNMANIC_SCRIPT"
```

Rollback:

```sh
sudo rm -f "$SEERR_STACK_REMOTE_OVERSIZED_UNMANIC_CRON"
```

Removing the cron entry stops future prioritization. Existing Unmanic tasks are
not removed by rollback.
