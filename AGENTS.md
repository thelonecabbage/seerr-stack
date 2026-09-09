# Agent Instructions

This repository is the sanitized source of truth for the Seerr Portainer stack and its automation scripts. Agents may install, fix, and update this stack, but must keep runtime secrets and mutable application state out of Git.

## Scope

Tracked source:

- `docker-compose.yml`
- `.env.example`
- `scripts/seerr_queue_rescue.py`
- `cron/seerr-queue-rescue`
- `README.md`
- `docs/`
- `tools/`

Never commit runtime config or state:

- `.env`
- `/opt/media/config`
- qBittorrent config files
- Sonarr/Radarr/Prowlarr config XML files
- Seerr/Jellyseerr databases or app config
- SABnzbd config
- API keys, passwords, tokens, cookies, Usenet credentials, indexer credentials, or proxy credentials
- downloader state, torrent files, downloads, and media

## Hosts And Paths

Default live host:

```sh
justin@asimov.local
```

Default live paths:

```sh
/home/docker/volumes/portainer_data/_data/compose/6/docker-compose.yml
/opt/media/scripts/seerr_queue_rescue.py
/etc/cron.d/seerr-queue-rescue
/var/log/seerr-queue-rescue.log
```

The local repository path is normally:

```sh
/home/justin/Documents/ChatGPT/HomeAssistant/seerr-stack
```

## Required Workflow

For every automated install, fix, or update:

1. Inspect the live state before changing it.
2. Back up any live file before replacing it.
3. Change the repository copy first when practical.
4. Run validation locally.
5. Install the changed file on Asimov.
6. Verify the live service or script behavior.
7. Pull the live state back into the repository if the live copy was edited directly.
8. Run `gitleaks detect`.
9. Commit the repository change.
10. Push `main` to `origin`.

Do not finish with uncommitted stack or script changes unless the user explicitly asks to stop before committing.

## Validation

Before committing, run:

```sh
docker compose config >/tmp/seerr-stack-compose-config.out
python3 -m py_compile scripts/seerr_queue_rescue.py
```

Then run a secret scan:

```sh
gitleaks detect --source . --redact
```

If local `gitleaks` is unavailable, use Docker:

```sh
docker run --rm -v "$PWD:/repo" ghcr.io/gitleaks/gitleaks:latest detect --source /repo --redact
```

If `gitleaks` reports a finding, do not commit or push. Remove the secret, rotate it if it touched Git history, and scan again.

## Live Install Commands

Install the rescue script:

```sh
scp scripts/seerr_queue_rescue.py justin@asimov.local:/tmp/seerr_queue_rescue.py
ssh justin@asimov.local 'sudo cp /opt/media/scripts/seerr_queue_rescue.py /opt/media/scripts/seerr_queue_rescue.py.bak-$(date +%Y%m%d-%H%M%S) && sudo install -m 0755 /tmp/seerr_queue_rescue.py /opt/media/scripts/seerr_queue_rescue.py'
```

Install the cron entry:

```sh
scp cron/seerr-queue-rescue justin@asimov.local:/tmp/seerr-queue-rescue
ssh justin@asimov.local 'sudo cp /etc/cron.d/seerr-queue-rescue /etc/cron.d/seerr-queue-rescue.bak-$(date +%Y%m%d-%H%M%S) && sudo install -m 0644 /tmp/seerr-queue-rescue /etc/cron.d/seerr-queue-rescue'
```

Validate the live rescue script:

```sh
ssh justin@asimov.local '/opt/media/scripts/seerr_queue_rescue.py --dry-run'
```

Deploy Compose changes through Portainer when possible. If using Docker Compose directly, validate on Asimov first:

```sh
ssh justin@asimov.local 'cd /home/docker/volumes/portainer_data/_data/compose/6 && sudo docker compose config'
```

Back up the live Compose file before replacing it.

## Sync From Asimov

Use this helper after live edits or before committing if there is any chance the live stack changed:

```sh
tools/sync_from_asimov.sh
```

The helper fetches the live Compose file, cron file, and rescue script from Asimov, runs `gitleaks detect`, and commits changed tracked files if the scan passes.

## Queue Rescue Rules

The rescue script must stay conservative:

- Use actual Sonarr air dates and Radarr release dates for recent-release prioritization.
- Keep per-run rescue caps small.
- Group duplicate queue rows by download ID.
- Treat qBittorrent metadata-only downloads as stale candidates when Sonarr/Radarr report them as stuck.
- Treat completed executable payloads as unsafe failures.
- Do not delete imported media.
- Do not print API keys or bearer tokens.

## Git Rules

Use normal non-destructive Git operations.

```sh
git status --short --branch
git add .
git commit -m "Describe the stack change"
git push origin main
```

Never force-push unless the user explicitly requests it after being told what will be overwritten.

