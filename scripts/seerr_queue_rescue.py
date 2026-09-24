#!/usr/bin/env python3
"""Observe stalled torrents, replace dead releases, and recover verified imports."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


CONFIG_DIR = Path(os.environ.get("SEERR_RESCUE_CONFIG_DIR", "/srv/media-stack/config"))
STATE_PATH = Path(os.environ.get("SEERR_RESCUE_STATE_FILE", "/var/lib/seerr-queue-rescue/state.json"))

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


def qbit_torrents_by_hash() -> dict[str, dict[str, Any]]:
    torrents = qbit_request("/api/v2/torrents/info")
    if not isinstance(torrents, list):
        raise RuntimeError("invalid qBittorrent snapshot")
    return {
        str(torrent.get("hash")).lower(): torrent
        for torrent in torrents
        if torrent.get("hash")
    }


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


def is_candidate(
    app: str,
    item: dict[str, Any],
    recent_hours: int,
    old_hours: int,
    release_window_hours: int,
    now: datetime,
    qbit_torrent: dict[str, Any] | None,
    observation: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    if item.get("protocol") != "torrent":
        return False, "not-torrent"
    if item.get("downloadClient") != "qBittorrent":
        return False, "not-qbittorrent"

    if item.get("status") == "completed" or (qbit_torrent and qbit_torrent.get("progress", 0) >= 1):
        return False, "completed-protected"
    if not qbit_torrent or not observation or not observation.get("valid"):
        return False, "missing-observation"
    if observation.get("kind") is None:
        return False, "progressing-or-ineligible-state"
    since = observation.get("since", now.timestamp())
    stalled_hours = max(0, (now.timestamp() - since) / 3600)
    kind = observation["kind"]
    if kind == "metadata":
        threshold = env_int("SEERR_RESCUE_METADATA_HOURS", 2)
    elif kind == "dead":
        threshold = env_int("SEERR_RESCUE_ZERO_AVAIL_HOURS", 4)
    else:
        threshold = old_hours
        recent = is_recent_release(app, item, now, release_window_hours)
        if recent:
            threshold = recent_hours
    return stalled_hours >= threshold, f"{kind} stalled={stalled_hours:.1f}h threshold={threshold}h"


def queue_records(app: str) -> list[dict[str, Any]]:
    data = request(app, "GET", APPS[app]["queue_path"])
    if isinstance(data, dict):
        if data.get("totalRecords", 0) > len(data.get("records", [])):
            raise RuntimeError(f"{app}: incomplete queue snapshot")
        return list(data.get("records") or [])
    if isinstance(data, list):
        return data
    raise RuntimeError(f"{app}: invalid queue snapshot")


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


def log(message: str) -> None:
    print(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {message}", flush=True)


def save_state(state: dict[str, Any]) -> None:
    temporary = STATE_PATH.with_suffix(".tmp")
    with temporary.open("w") as stream:
        os.chmod(temporary, 0o600)
        json.dump(state, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(STATE_PATH)


def container_info(app: str) -> dict[str, Any]:
    name = os.environ.get(f"SEERR_RESCUE_{app.upper()}_CONTAINER", app)
    result = subprocess.run(["docker", "inspect", name], capture_output=True, text=True, timeout=15, check=True)
    info = json.loads(result.stdout)[0]
    if not info["State"]["Running"]:
        raise RuntimeError(f"{app}: container not running")
    return info


def observe(state: dict[str, Any], torrents: dict[str, dict[str, Any]], now: float, restart: str) -> None:
    previous = state.get("torrents", {})
    reset = state.get("restart") != restart or not 0 <= now - state.get("observed", 0) <= 4500
    started = parse_time(restart)
    if not started:
        raise RuntimeError("qbit: restart time unavailable")
    grace = now - started.timestamp() < env_int("SEERR_RESCUE_RESTART_GRACE_MINUTES", 30) * 60
    observed = {}
    for h, torrent in torrents.items():
        old = {} if reset else previous.get(h, {})
        required = ("downloaded", "progress", "dlspeed", "num_seeds", "num_leechs", "availability", "last_activity")
        valid = all(isinstance(torrent.get(k), (int, float)) and torrent[k] >= 0 for k in required)
        moving = not valid or torrent["dlspeed"] > 0 or any(
            torrent[k] > old.get(k, torrent[k]) for k in ("downloaded", "progress", "last_activity")
        )
        kind = None
        if valid and not moving and not grace:
            if torrent.get("state") == "metaDL" and torrent["num_seeds"] == torrent["num_leechs"] == 0:
                kind = "metadata"
            elif torrent.get("state") == "stalledDL" and torrent["progress"] < 1:
                kind = "dead" if torrent["progress"] == torrent["num_seeds"] == torrent["availability"] == 0 else "stalled"
        observed[h] = {
            **{k: torrent.get(k) for k in required},
            "state": torrent.get("state"), "valid": valid, "kind": kind,
            "since": old.get("since", now) if kind is not None and old.get("kind") == kind else now,
            "last_progress": now if moving or not old else old.get("last_progress", now),
        }
    state.update(torrents=observed, observed=now, restart=restart)


def identities(app: str, rows: list[dict[str, Any]]) -> list[int]:
    key = APPS[app]["id_key"]
    if any(not row.get(key) for row in rows):
        return []
    return sorted({int(row[key]) for row in rows})


def cooldown(state: dict[str, Any], app: str, ids: list[int], now: float) -> bool:
    maximum = env_int("SEERR_RESCUE_MAX_REPLACEMENTS_24H", 2)
    return any(sum(at > now - 86400 for at in state.get("attempts", {}).get(f"{app}:{i}", [])) >= maximum for i in ids)


def reserve(state: dict[str, Any], app: str, ids: list[int], h: str, now: float) -> None:
    for i in ids:
        attempts = state.setdefault("attempts", {}).setdefault(f"{app}:{i}", [])
        attempts[:] = [at for at in attempts if at > now - 86400] + [now]
    # Persist before network writes. An ambiguous failure must not repeat deletion/search.
    state.setdefault("rescued", {})[f"{app}:{h}"] = {"at": now, "phase": "reserved"}
    save_state(state)


def rescue(app: str, rows: list[dict[str, Any]], torrents: dict[str, dict[str, Any]],
           state: dict[str, Any], dry_run: bool, now: datetime) -> None:
    meta = APPS[app]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        h = download_hash(row)
        if h:
            grouped.setdefault(h, []).append(row)
    selected = 0
    for h, group in grouped.items():
        results = [is_candidate(app, row,
                   env_int(meta["recent_hours_env"], meta["default_recent_hours"]),
                   env_int(meta["old_hours_env"], meta["default_old_hours"]),
                   env_int(meta["release_window_env"], meta["default_release_window"]),
                   now, torrents.get(h), state["torrents"].get(h)) for row in group]
        ok = all(result[0] for result in results)
        reason = next((reason for eligible, reason in results if not eligible), results[0][1])
        ids = identities(app, group)
        if ok and (not ids or f"{app}:{h}" in state.get("rescued", {})):
            ok, reason = False, "missing-identity-or-previous-attempt-needs-review"
        if ok and cooldown(state, app, ids, now.timestamp()):
            ok, reason = False, "cooldown: replacement limit reached in last 24h"
        if ok and selected >= env_int(meta["max_env"], meta["default_max"]):
            ok, reason = False, "per-run-cap"
        log(f"{app}: {'would rescue' if dry_run and ok else 'rescue' if ok else 'skip'} hash={h} {reason} title={title(group[0])}")
        if not ok:
            continue
        selected += 1
        if dry_run:
            # Simulate reservations in memory so a dry run applies the same caps.
            for i in ids:
                state.setdefault("attempts", {}).setdefault(f"{app}:{i}", []).append(now.timestamp())
            continue
        fresh = qbit_torrents_by_hash().get(h)
        if not fresh or any(fresh.get(k) != torrents[h].get(k) for k in
                            ("state", "downloaded", "progress", "last_activity", "num_seeds", "num_leechs", "availability")) or fresh.get("dlspeed", 1) > 0:
            log(f"{app}: skip hash={h} changed-since-observation")
            continue
        if container_info("qbittorrent")["State"]["StartedAt"] != state["restart"]:
            log(f"{app}: skip hash={h} qBittorrent restarted during run")
            continue
        reserve(state, app, ids, h, now.timestamp())
        params = urllib.parse.urlencode({"removeFromClient": "true", "blocklist": "true", "skipRedownload": "true"})
        request(app, "DELETE", f"/api/v3/queue/{group[0]['id']}?{params}")
        state["rescued"][f"{app}:{h}"]["phase"] = "removed"
        save_state(state)
        command = request(app, "POST", "/api/v3/command", {"name": meta["search_command"], meta["search_key"]: ids})
        state["rescued"][f"{app}:{h}"].update(phase="search-submitted", command_id=command.get("id"))
        save_state(state)
        log(f"{app}: replacement search submitted hash={h} command_id={command.get('id')}")
    log(f"{app}: rescue summary groups={len(grouped)} selected={selected}")


def exec_check(info: dict[str, Any], script: str, *args: str) -> str:
    env = dict(value.split("=", 1) for value in info["Config"]["Env"] if "=" in value)
    uid, gid = env.get("PUID"), env.get("PGID")
    if not uid or not gid or not uid.isdigit() or not gid.isdigit() or uid == "0":
        raise RuntimeError("container application identity unavailable")
    result = subprocess.run(["docker", "exec", "--user", f"{uid}:{gid}", info["Id"],
                             "sh", "-c", script, "rescue", *args], capture_output=True, text=True, timeout=20)
    if result.returncode:
        raise RuntimeError("container path missing, empty, or inaccessible")
    return result.stdout.strip()


def check_path(info: dict[str, Any], path: str, mode: str) -> None:
    p = PurePosixPath(path)
    if not p.is_absolute() or ".." in p.parts:
        raise RuntimeError("invalid media path")
    mounts = [m for m in info["Mounts"] if m["Type"] == "bind" and m["Destination"] != "/config"
              and p.is_relative_to(m["Destination"])]
    if not mounts:
        raise RuntimeError("media path has no verified bind mount")
    mount = max(mounts, key=lambda m: len(m["Destination"]))
    if mode == "destination" and not mount["RW"]:
        raise RuntimeError("destination mount is read-only")
    fs = subprocess.run(["findmnt", "--json", "--target", mount["Source"], "--output", "TARGET"],
                        capture_output=True, text=True, timeout=10, check=True)
    if json.loads(fs.stdout)["filesystems"][0]["target"] == "/":
        raise RuntimeError("data mount resolves to host root filesystem; inspect mount")
    host_stat = os.stat(mount["Source"])
    container_stat = exec_check(info, 'stat -c "%d:%i" -- "$1"', mount["Destination"])
    if container_stat != f"{host_stat.st_dev}:{host_stat.st_ino}":
        raise RuntimeError("host/container mount identity mismatch")
    # Resolve symlinks in-container and require that they stay inside the checked mount.
    resolved = exec_check(info, 'readlink -m -- "$1"', path)
    if not PurePosixPath(resolved).is_relative_to(mount["Destination"]):
        raise RuntimeError("media path escapes verified mount")
    if mode == "file":
        exec_check(info, '[ -f "$1" ] && [ -s "$1" ] && [ -r "$1" ]', path)
    elif mode == "source":
        exec_check(info, 'if [ -f "$1" ]; then test -s "$1" && test -r "$1"; '
                   'else test -d "$1" && test -r "$1" && test -x "$1"; fi', path)
    else:
        exec_check(info, 'p="$1"; while [ ! -e "$p" ] && [ "$p" != "$2" ]; do p=$(dirname -- "$p"); done; '
                   'test -d "$p" && test -w "$p" && test -x "$p"', path, mount["Destination"])


def import_history(app: str, h: str) -> list[dict[str, Any]]:
    data = request(app, "GET", "/api/v3/history?" + urllib.parse.urlencode({"downloadId": h, "pageSize": 1000, "page": 1}))
    if not isinstance(data, dict) or data.get("totalRecords", 0) > len(data.get("records", [])):
        raise RuntimeError("import history incomplete")
    return [r for r in data.get("records", []) if str(r.get("downloadId", "")).lower() == h
            and r.get("eventType") == "downloadFolderImported"]


def verified_imports(app: str, h: str, rows: list[dict[str, Any]], info: dict[str, Any]) -> bool:
    ids = identities(app, rows)
    if not ids:
        return False
    history = import_history(app, h)
    key = APPS[app]["id_key"]
    for i in ids:
        matches = [r for r in history if r.get(key) == i]
        if not matches:
            return False
        entity = request(app, "GET", f"/api/v3/{'episode' if app == 'sonarr' else 'movie'}/{i}")
        file_id = entity.get("episodeFileId" if app == "sonarr" else "movieFileId")
        matching = [r for r in matches if str(r.get("data", {}).get("fileId")) == str(file_id)]
        if not file_id or not matching:
            return False
        file = request(app, "GET", f"/api/v3/{'episodefile' if app == 'sonarr' else 'moviefile'}/{file_id}")
        if not file.get("path") or not any(r.get("data", {}).get("importedPath") == file["path"] for r in matching):
            return False
        check_path(info, file["path"], "file")
    return True


def video_paths(torrent: dict[str, Any], files: list[dict[str, Any]]) -> list[str]:
    if not torrent.get("save_path") or not files:
        raise RuntimeError("missing source file list or save path")
    paths = []
    for file in files:
        name = PurePosixPath(file["name"])
        if name.is_absolute() or ".." in name.parts or name.suffix.lower() in {".exe", ".scr", ".com", ".bat", ".msi"}:
            raise RuntimeError("invalid or executable payload")
        if file.get("priority", 0) > 0 and name.suffix.lower() in {".mkv", ".mp4", ".avi", ".m4v", ".ts"}:
            if file.get("progress") != 1:
                raise RuntimeError("source video incomplete")
            paths.append(str(PurePosixPath(torrent["save_path"]) / name))
    if not paths:
        raise RuntimeError("no selected video files to verify")
    return paths


def verified_payload(app: str, h: str, torrent: dict[str, Any], rows: list[dict[str, Any]]) -> bool:
    paths = video_paths(torrent, qbit_request("/api/v2/torrents/files", {"hash": h}))
    ids = identities(app, rows)
    history = import_history(app, h)
    imported = {record.get("data", {}).get("droppedPath") for record in history
                if record.get(APPS[app]["id_key"]) in ids}
    return all(path in imported for path in paths)


def completed(app: str, rows: list[dict[str, Any]], torrents: dict[str, dict[str, Any]],
              state: dict[str, Any], dry_run: bool, now: float) -> None:
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        h = download_hash(row)
        if h and row.get("protocol") == "torrent" and row.get("downloadClient") == "qBittorrent":
            groups.setdefault(h, []).append(row)
    info = None
    actions = 0
    for h, group in groups.items():
        if not all(r.get("status") == "completed" for r in group):
            continue
        if actions >= env_int("SEERR_RESCUE_MAX_IMPORT_ACTIONS", 2):
            log(f"{app}: import skip hash={h} per-run-cap")
            continue
        key = f"{app}:{h}"
        retry = state.setdefault("imports", {}).setdefault(key, {"first_seen": now, "attempts": 0})
        try:
            info = info or container_info(app)
            torrent = torrents.get(h)
            if not torrent or torrent.get("progress") != 1 or torrent.get("state") not in {"stoppedUP", "pausedUP", "stalledUP", "uploading", "forcedUP", "queuedUP"}:
                log(f"{app}: import skip hash={h} missing-or-busy-torrent")
                continue
            if verified_imports(app, h, group, info):
                if not verified_payload(app, h, torrent, group):
                    log(f"{app}: import review hash={h} torrent has video files without matching import history; retained")
                    continue
                enabled = bool(env_int("SEERR_RESCUE_CLEANUP_ENABLED", 0))
                log(f"{app}: {'would clear' if dry_run or not enabled else 'clear'} verified imported group hash={h}; keep all files enabled={enabled}")
                if enabled and not dry_run:
                    # Detach first with deleteFiles=false; Arr removal must not touch the client.
                    qbit_request("/api/v2/torrents/delete", {"hashes": h, "deleteFiles": "false"}, method="POST")
                    params = urllib.parse.urlencode({"removeFromClient": "false", "blocklist": "false", "skipRedownload": "true"})
                    request(app, "DELETE", f"/api/v3/queue/{group[0]['id']}?{params}")
                    retry["cleaned"] = now
                    save_state(state)
                actions += 1
                continue
            if any("already imported" in status_text(r) for r in group):
                log(f"{app}: import review hash={h} incomplete history/file proof for entire group; retained")
                continue
            if any(UNSAFE_FILE_RE.search(status_text(r)) for r in group):
                log(f"{app}: import review hash={h} unsafe executable reported; retained without import")
                continue
            if any(r.get("trackedDownloadState") == "importing" for r in group):
                log(f"{app}: import skip hash={h} import already running")
                continue
            if now - retry["first_seen"] < env_int("SEERR_RESCUE_IMPORT_GRACE_MINUTES", 60) * 60:
                log(f"{app}: import skip hash={h} initial import grace")
                continue
            if retry["attempts"] >= env_int("SEERR_RESCUE_MAX_IMPORT_RETRIES", 2) or now - retry.get("last_attempt", 0) < 21600:
                log(f"{app}: import review hash={h} retry limit or 6h cooldown")
                continue
            commands = request(app, "GET", "/api/v3/command")
            if any(c.get("status") in {"queued", "started"} and c.get("name") in
                   {"DownloadedMoviesScan", "DownloadedEpisodesScan", "ProcessMonitoredDownloads", "ImportListSync", "RefreshMovie", "RefreshSeries"} for c in commands):
                log(f"{app}: import skip hash={h} application scan active")
                continue
            source = group[0].get("outputPath")
            if not source or any(r.get("outputPath") != source for r in group):
                raise RuntimeError("missing or inconsistent source path")
            check_path(info, source, "source")
            files = qbit_request("/api/v2/torrents/files", {"hash": h})
            for path in video_paths(torrent, files):
                check_path(info, path, "file")
            for row in group:
                entity_id = row.get("seriesId" if app == "sonarr" else "movieId")
                if not entity_id:
                    raise RuntimeError("missing library identity")
                entity = request(app, "GET", f"/api/v3/{'series' if app == 'sonarr' else 'movie'}/{entity_id}")
                check_path(info, entity.get("path", ""), "destination")
            enabled = bool(env_int("SEERR_RESCUE_IMPORT_ENABLED", 0))
            log(f"{app}: {'would retry' if dry_run or not enabled else 'retry'} verified import hash={h} attempt={retry['attempts'] + 1} enabled={enabled}")
            if enabled and not dry_run:
                retry.update(attempts=retry["attempts"] + 1, last_attempt=now)
                save_state(state)
                command = request(app, "POST", "/api/v3/command", {
                    "name": "DownloadedEpisodesScan" if app == "sonarr" else "DownloadedMoviesScan",
                    "path": source, "downloadClientId": h.upper(), "importMode": "Copy",
                })
                retry["command_id"] = command.get("id")
                save_state(state)
                log(f"{app}: import command submitted hash={h} command_id={command.get('id')}")
            actions += 1
        except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as exc:
            log(f"{app}: import review hash={h} {type(exc).__name__}: {exc}")
    log(f"{app}: import summary selected={actions}")


def network_diagnostics() -> None:
    try:
        records = qbit_request("/api/v2/log/main", {"normal": "false", "info": "false", "warning": "true",
                               "critical": "true", "last_known_id": -1})
        failed = any("port mapping failed" in record.get("message", "").lower() for record in records[-200:])
        if failed:
            log("qbit: port mapping failure reported; incoming reachability unverified; router unchanged")
    except (OSError, ValueError, TypeError):
        log("qbit: optional port mapping diagnostic unavailable")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--app", choices=sorted(APPS), action="append")
    parser.add_argument("--no-promote", action="store_true")
    parser.add_argument("--observe", action="store_true", help="persist observations without application writes")
    args = parser.parse_args()

    apps = args.app or ["sonarr", "radarr"]
    dry_run = args.dry_run or args.observe
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with STATE_PATH.with_suffix(".lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                log("skip: another rescue run holds the lock")
                return 0
            state = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {"version": 1}
            if state.get("version") != 1:
                raise RuntimeError("unsupported state version")
            now = datetime.now(timezone.utc)
            torrents = qbit_torrents_by_hash()
            restart = container_info("qbittorrent")["State"]["StartedAt"]
            observe(state, torrents, now.timestamp(), restart)
            queues = {app: queue_records(app) for app in apps}
            network_diagnostics()
            if not args.dry_run:
                save_state(state)
            for line in ensure_qbit_preferences(dry_run):
                log(line)
            if not args.no_promote:
                for line in promote_recent(apps, dry_run, now):
                    log(line)
            for app in apps:
                completed(app, queues[app], torrents, state, dry_run, now.timestamp())
            # Save observations, never simulated replacement attempts from a dry run.
            if not args.dry_run:
                save_state(state)
            for app in apps:
                rescue(app, queues[app], torrents, state, dry_run, now)
            log(f"summary torrents={len(torrents)} active={sum(t.get('dlspeed', 0) > 0 for t in torrents.values())} "
                f"dead_or_metadata={sum(o['kind'] in {'dead', 'metadata'} for o in state['torrents'].values())} dry_run={dry_run}")
    except (urllib.error.URLError, RuntimeError, OSError, ValueError, subprocess.SubprocessError) as exc:
        log(f"ERROR: {type(exc).__name__}: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
