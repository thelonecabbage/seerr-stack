#!/usr/bin/env python3
"""Blocklist stalled Sonarr/Radarr torrents and trigger a fresh search.

This is intentionally conservative:
- completed/import-pending items are skipped
- young downloads are skipped
- duplicate queue rows for the same torrent are grouped
- each run has a small delete/search cap
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CONFIG_DIR = Path(os.environ.get("SEERR_RESCUE_CONFIG_DIR", "/srv/media-stack/config"))

APPS = {
    "sonarr": {
        "port": 8989,
        "config": CONFIG_DIR / "sonarr/config.xml",
        "queue_path": "/api/v3/queue?page=1&pageSize=1000&includeUnknownSeriesItems=true",
        "search_command": "EpisodeSearch",
        "search_key": "episodeIds",
        "id_key": "episodeId",
        "max_env": "SEERR_RESCUE_MAX_SONARR",
        "default_max": 5,
        "recent_hours_env": "SEERR_RESCUE_SONARR_RECENT_HOURS",
        "old_hours_env": "SEERR_RESCUE_SONARR_OLD_HOURS",
        "default_recent_hours": 2,
        "default_old_hours": 48,
        "release_window_env": "SEERR_RESCUE_SONARR_RELEASE_WINDOW_HOURS",
        "default_release_window": 336,
    },
    "radarr": {
        "port": 7878,
        "config": CONFIG_DIR / "radarr/config.xml",
        "queue_path": "/api/v3/queue?page=1&pageSize=1000&includeMovie=true",
        "search_command": "MoviesSearch",
        "search_key": "movieIds",
        "id_key": "movieId",
        "max_env": "SEERR_RESCUE_MAX_RADARR",
        "default_max": 3,
        "recent_hours_env": "SEERR_RESCUE_RADARR_RECENT_HOURS",
        "old_hours_env": "SEERR_RESCUE_RADARR_OLD_HOURS",
        "default_recent_hours": 4,
        "default_old_hours": 48,
        "release_window_env": "SEERR_RESCUE_RADARR_RELEASE_WINDOW_HOURS",
        "default_release_window": 720,
    },
}

STALE_TERMS = (
    "stalled",
    "no connections",
    "no seeds",
    "timed out",
    "torrent not registered",
    "tracker",
)

UNSAFE_FILE_RE = re.compile(r"(^|[\s/\\])[^/\\]+\.exe($|[\s/\\])", re.IGNORECASE)

QBIT_PREFS = {
    "queueing_enabled": True,
    "dont_count_slow_torrents": True,
    "max_active_downloads": 16,
    "max_active_torrents": 40,
    "max_active_uploads": 8,
    "max_connec": 2000,
    "max_connec_per_torrent": 80,
    "max_uploads": 60,
    "max_uploads_per_torrent": 8,
    "scheduler_enabled": False,
    "dl_limit": 0,
    "up_limit": 1048576,
    "alt_dl_limit": 0,
    "alt_up_limit": 131072,
}

EPISODE_CACHE: dict[int, dict[str, Any]] = {}


def env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return int(value)


def api_key(config_path: Path) -> str:
    root = ET.parse(config_path).getroot()
    key = root.findtext("ApiKey")
    if not key:
        raise RuntimeError(f"missing ApiKey in {config_path}")
    return key


def qbit_api_key() -> str:
    config = CONFIG_DIR / "qbittorrent/qBittorrent/qBittorrent.conf"
    for line in config.read_text().splitlines():
        if line.startswith("WebUI\\APIKey="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError(f"missing WebUI API key in {config}")


def request(app: str, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    meta = APPS[app]
    data = None
    headers = {"X-Api-Key": api_key(meta["config"])}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        f"http://127.0.0.1:{meta['port']}{path}",
        data=data,
        method=method,
        headers=headers,
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        payload = response.read()
    if not payload:
        return None
    return json.loads(payload)


def qbit_post_json(path: str, payload: dict[str, Any]) -> None:
    body = urllib.parse.urlencode({"json": json.dumps(payload)}).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:8081{path}",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {qbit_api_key()}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        response.read()


def qbit_text(path: str) -> str:
    req = urllib.request.Request(
        f"http://127.0.0.1:8081{path}",
        method="GET",
        headers={"Authorization": f"Bearer {qbit_api_key()}"},
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        return response.read().decode().strip()


def ensure_qbit_preferences(dry_run: bool) -> list[str]:
    prefs = qbit_request("/api/v2/app/preferences")
    if not isinstance(prefs, dict):
        return ["qbit: could not read preferences"]

    changes = {
        key: value
        for key, value in QBIT_PREFS.items()
        if prefs.get(key) != value
    }
    mode = qbit_text("/api/v2/transfer/speedLimitsMode")
    mode_change = mode != "0"

    if not changes and not mode_change:
        return ["qbit: preferences already healthy"]

    lines = []
    if changes:
        names = ", ".join(sorted(changes))
        lines.append(f"qbit: {'would update' if dry_run else 'updating'} preferences: {names}")
        if not dry_run:
            qbit_post_json("/api/v2/app/setPreferences", changes)
    if mode_change:
        lines.append(f"qbit: {'would disable' if dry_run else 'disabling'} alternative speed limits")
        if not dry_run:
            qbit_request("/api/v2/transfer/toggleSpeedLimitsMode", method="POST")
    return lines


def qbit_request(path: str, params: dict[str, Any] | None = None, method: str = "GET") -> Any:
    url = f"http://127.0.0.1:8081{path}"
    data = None
    headers = {"Authorization": f"Bearer {qbit_api_key()}"}
    if params is not None:
        encoded = urllib.parse.urlencode(params, doseq=True).encode()
        if method == "GET":
            url += "?" + encoded.decode()
        else:
            data = encoded
            headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as response:
        payload = response.read()
    if not payload:
        return None
    return json.loads(payload)


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return None


def age_hours(value: str | None, now: datetime) -> float:
    parsed = parse_time(value)
    if not parsed:
        return 0.0
    return max(0.0, (now - parsed.astimezone(timezone.utc)).total_seconds() / 3600)


def newest_release_time(app: str, item: dict[str, Any]) -> datetime | None:
    values: list[str] = []
    if app == "sonarr":
        for episode in item.get("episodes") or []:
            values.extend(
                x
                for x in [episode.get("airDateUtc"), episode.get("airDate")]
                if x
            )
        episode = item.get("episode") or {}
        values.extend(x for x in [episode.get("airDateUtc"), episode.get("airDate")] if x)
        episode_id = item.get("episodeId")
        if episode_id and not values:
            episode_id = int(episode_id)
            if episode_id not in EPISODE_CACHE:
                EPISODE_CACHE[episode_id] = request("sonarr", "GET", f"/api/v3/episode/{episode_id}")
            episode = EPISODE_CACHE[episode_id]
            values.extend(x for x in [episode.get("airDateUtc"), episode.get("airDate")] if x)
    else:
        movie = item.get("movie") or {}
        values.extend(
            x
            for x in [
                movie.get("digitalRelease"),
                movie.get("physicalRelease"),
                movie.get("inCinemas"),
            ]
            if x
        )
    parsed = [x for x in (parse_time(v) for v in values) if x]
    return max(parsed) if parsed else None


def is_recent_release(app: str, item: dict[str, Any], now: datetime, release_window_hours: int) -> bool:
    released = newest_release_time(app, item)
    if not released:
        return False
    return 0 <= (now - released.astimezone(timezone.utc)).total_seconds() / 3600 <= release_window_hours


def status_text(item: dict[str, Any]) -> str:
    parts = [
        str(item.get("status") or ""),
        str(item.get("errorMessage") or ""),
        str(item.get("trackedDownloadState") or ""),
        str(item.get("trackedDownloadStatus") or ""),
        str(item.get("title") or ""),
        str(item.get("outputPath") or ""),
    ]
    for msg in item.get("statusMessages") or []:
        if isinstance(msg, dict):
            parts.append(str(msg.get("title") or ""))
            parts.extend(str(x) for x in msg.get("messages") or [])
    return " ".join(parts).lower()


def is_candidate(app: str, item: dict[str, Any], recent_hours: int, old_hours: int, release_window_hours: int, now: datetime) -> tuple[bool, str]:
    if item.get("protocol") != "torrent":
        return False, "not-torrent"
    if item.get("downloadClient") != "qBittorrent":
        return False, "not-qbittorrent"

    age = age_hours(item.get("added"), now)
    text = status_text(item)
    unsafe_executable = bool(UNSAFE_FILE_RE.search(text))
    if unsafe_executable and item.get("status") in {"completed", "warning"}:
        return True, f"unsafe_executable=True age={age:.1f}h"

    metadata_only = "downloading metadata" in text
    if item.get("sizeleft") == 0 and not metadata_only:
        return False, "nothing-left"

    stale_signal = any(term in text for term in STALE_TERMS)
    no_eta = item.get("timeleft") in (None, "", "00:00:00")
    warning = item.get("status") in {"warning", "queued"}

    recent = is_recent_release(app, item, now, release_window_hours)
    threshold = recent_hours if recent else old_hours
    if warning and (metadata_only or stale_signal or no_eta) and age >= threshold:
        return True, f"age={age:.1f}h threshold={threshold}h recent_release={recent} metadata_only={metadata_only}"
    return False, f"age={age:.1f}h threshold={threshold}h recent_release={recent}"


def queue_records(app: str) -> list[dict[str, Any]]:
    data = request(app, "GET", APPS[app]["queue_path"])
    if isinstance(data, dict):
        return list(data.get("records") or [])
    if isinstance(data, list):
        return data
    return []


def title(item: dict[str, Any]) -> str:
    value = item.get("title") or ""
    return " ".join(str(value).split())[:120]


def download_hash(item: dict[str, Any]) -> str | None:
    value = str(item.get("downloadId") or "")
    match = re.search(r"[a-fA-F0-9]{40}", value)
    if match:
        return match.group(0).lower()
    if re.fullmatch(r"[a-fA-F0-9]{32}", value):
        return value.lower()
    return None


def promote_recent(apps: list[str], dry_run: bool, now: datetime) -> list[str]:
    hashes: OrderedDict[str, str] = OrderedDict()
    for app in apps:
        meta = APPS[app]
        release_window = env_int(meta["release_window_env"], meta["default_release_window"])
        for item in queue_records(app):
            if item.get("protocol") != "torrent" or item.get("downloadClient") != "qBittorrent":
                continue
            if not is_recent_release(app, item, now, release_window):
                continue
            h = download_hash(item)
            if h:
                hashes[h] = f"{app}: {title(item)}"

    if not hashes:
        return ["qbit: 0 recent torrent(s) selected for top priority"]

    lines = [f"qbit: {'would promote' if dry_run else 'promoting'} {len(hashes)} recent torrent(s) to top priority"]
    for reason in list(hashes.values())[:10]:
        lines.append(f"qbit: recent {reason}")
    if not dry_run:
        qbit_request("/api/v2/torrents/topPrio", {"hashes": "|".join(hashes)}, method="POST")
    return lines


def rescue(app: str, dry_run: bool, now: datetime) -> list[str]:
    meta = APPS[app]
    max_items = env_int(meta["max_env"], meta["default_max"])
    recent_hours = env_int(meta["recent_hours_env"], meta["default_recent_hours"])
    old_hours = env_int(meta["old_hours_env"], meta["default_old_hours"])
    release_window = env_int(meta["release_window_env"], meta["default_release_window"])

    grouped: OrderedDict[str, dict[str, Any]] = OrderedDict()
    for item in sorted(queue_records(app), key=lambda x: parse_time(x.get("added")) or datetime.min.replace(tzinfo=timezone.utc), reverse=True):
        ok, reason = is_candidate(app, item, recent_hours, old_hours, release_window, now)
        if not ok:
            continue
        download_id = item.get("downloadId") or str(item.get("id"))
        if download_id not in grouped:
            grouped[download_id] = {"queue_id": item.get("id"), "ids": set(), "title": title(item), "reason": reason}
        wanted_id = item.get(meta["id_key"])
        if wanted_id:
            grouped[download_id]["ids"].add(int(wanted_id))

    actions = list(grouped.values())[:max_items]
    lines = [f"{app}: {len(actions)} action(s) selected from {len(grouped)} stalled torrent group(s)"]
    for action in actions:
        ids = sorted(action["ids"])
        lines.append(f"{app}: {'would rescue' if dry_run else 'rescuing'} queue_id={action['queue_id']} ids={ids} {action['reason']} title={action['title']}")
        if dry_run:
            continue

        params = urllib.parse.urlencode(
            {"removeFromClient": "true", "blocklist": "true", "skipRedownload": "false"}
        )
        request(app, "DELETE", f"/api/v3/queue/{action['queue_id']}?{params}")
        if ids:
            request(app, "POST", "/api/v3/command", {"name": meta["search_command"], meta["search_key"]: ids})
            time.sleep(2)
    return lines


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--app", choices=sorted(APPS), action="append")
    parser.add_argument("--no-promote", action="store_true")
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    apps = args.app or ["sonarr", "radarr"]
    try:
        for line in ensure_qbit_preferences(args.dry_run):
            print(line, flush=True)
        if not args.no_promote:
            for line in promote_recent(apps, args.dry_run, now):
                print(line, flush=True)
        for app in apps:
            for line in rescue(app, args.dry_run, now):
                print(line, flush=True)
    except (urllib.error.URLError, urllib.error.HTTPError, RuntimeError, OSError) as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
