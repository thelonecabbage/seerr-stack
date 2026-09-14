#!/usr/bin/env python3
"""Prioritize oversized Radarr movie imports in Unmanic after they are stable."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


CONFIG_DIR = Path(os.environ.get("OVERSIZED_RADARR_CONFIG_DIR", "/srv/media-stack/config"))
RADARR_CONFIG = Path(os.environ.get("OVERSIZED_RADARR_CONFIG", CONFIG_DIR / "radarr/config.xml"))
STATE_PATH = Path(
    os.environ.get("OVERSIZED_RADARR_UNMANIC_STATE", "/var/lib/focus-oversized-radarr-unmanic/state.json")
)
UNMANIC_DB = Path(os.environ.get("OVERSIZED_UNMANIC_DB", "/srv/unmanic/config/config/unmanic.db"))
RADARR_BASE = os.environ.get("OVERSIZED_RADARR_URL", "http://127.0.0.1:7878/api/v3").rstrip("/")
RADARR_MOVIES_PREFIX = os.environ.get("OVERSIZED_RADARR_MOVIES_PREFIX", "/movies/")
UNMANIC_MOVIES_PREFIX = os.environ.get("OVERSIZED_UNMANIC_MOVIES_PREFIX", "/media/movies/")
LARGE_BYTES = int(os.environ.get("OVERSIZED_MOVIE_MIN_BYTES", str(40 * 1024**3)))
MIN_FILE_AGE_SECONDS = int(os.environ.get("OVERSIZED_MIN_FILE_AGE_SECONDS", str(30 * 60)))
STABILITY_SECONDS = int(os.environ.get("OVERSIZED_STABILITY_SECONDS", "20"))
LIBRARY_ID = int(os.environ.get("OVERSIZED_UNMANIC_LIBRARY_ID", "1"))
PRIORITY_BOOST = int(os.environ.get("OVERSIZED_UNMANIC_PRIORITY_BOOST", "1000000"))


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def log(message: str) -> None:
    print(f"{now_iso()} {message}", flush=True)


def read_api_key() -> str:
    root = ET.parse(RADARR_CONFIG).getroot()
    key = root.findtext("ApiKey")
    if not key:
        raise RuntimeError(f"Radarr API key not found in {RADARR_CONFIG}")
    return key


def radarr_get(api_key: str, path: str) -> Any:
    request = urllib.request.Request(
        RADARR_BASE + path,
        headers={"X-Api-Key": api_key, "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {"version": 1, "tracked": {}, "created_at": now_iso()}
    return json.loads(STATE_PATH.read_text())


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    tmp.replace(STATE_PATH)


def records_from_queue(queue: Any) -> list[dict[str, Any]]:
    if isinstance(queue, dict):
        return queue.get("records", [])
    return []


def history_for_download(api_key: str, download_id: str) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode(
        {
            "page": 1,
            "pageSize": 100,
            "sortKey": "date",
            "sortDirection": "descending",
            "downloadId": download_id,
        }
    )
    data = radarr_get(api_key, f"/history?{query}")
    return records_from_queue(data)


def movie_id_from_history(records: list[dict[str, Any]]) -> int | None:
    for record in records:
        movie = record.get("movie") or {}
        movie_id = record.get("movieId") or movie.get("id")
        if movie_id:
            return int(movie_id)
    return None


def safe_title(value: str | None) -> str:
    if not value:
        return "unknown"
    return re.sub(r"\s+", " ", value).strip()[:180]


def discover_large_queue(api_key: str, state: dict[str, Any]) -> tuple[int, set[str]]:
    queue = radarr_get(api_key, "/queue?page=1&pageSize=1000&includeUnknownMovieItems=true")
    active_download_ids: set[str] = set()
    added = 0
    tracked = state.setdefault("tracked", {})

    for item in records_from_queue(queue):
        download_id = item.get("downloadId")
        if not download_id:
            continue
        active_download_ids.add(download_id)
        size = int(item.get("size") or 0)
        if size < LARGE_BYTES or download_id in tracked:
            continue

        history = history_for_download(api_key, download_id)
        movie_id = movie_id_from_history(history)
        tracked[download_id] = {
            "download_id": download_id,
            "movie_id": movie_id,
            "title": safe_title(item.get("title") or (item.get("movie") or {}).get("title")),
            "quality": ((item.get("quality") or {}).get("quality") or {}).get("name"),
            "size": size,
            "status": "waiting_for_import",
            "first_seen": now_iso(),
            "last_seen_in_queue": now_iso(),
        }
        added += 1

    for download_id in active_download_ids:
        if download_id in tracked and tracked[download_id].get("status") == "waiting_for_import":
            tracked[download_id]["last_seen_in_queue"] = now_iso()

    return added, active_download_ids


def final_movie_path(api_key: str, movie_id: int) -> str | None:
    movie = radarr_get(api_key, f"/movie/{movie_id}")
    movie_file = movie.get("movieFile") or {}
    path = movie_file.get("path")
    if path:
        return path
    relative = movie_file.get("relativePath")
    root = movie.get("path")
    if relative and root:
        return os.path.join(root, relative)
    return None


def map_radarr_to_unmanic(path: str) -> str:
    if path.startswith(RADARR_MOVIES_PREFIX):
        return UNMANIC_MOVIES_PREFIX + path[len(RADARR_MOVIES_PREFIX) :]
    return path


def file_is_stable(path: str, *, dry_run: bool) -> tuple[bool, str]:
    try:
        first = os.stat(path)
    except FileNotFoundError:
        return False, "final file does not exist yet"
    if first.st_size < LARGE_BYTES:
        return False, "final file is below oversized threshold"
    age = time.time() - first.st_mtime
    if age < MIN_FILE_AGE_SECONDS:
        return False, f"final file is only {int(age)}s old"
    if dry_run:
        return True, "stable check skipped in dry-run after age/size gates"
    time.sleep(STABILITY_SECONDS)
    try:
        second = os.stat(path)
    except FileNotFoundError:
        return False, "final file disappeared during stability check"
    if (first.st_size, first.st_mtime_ns) != (second.st_size, second.st_mtime_ns):
        return False, "final file changed during stability check"
    return True, "final file is stable"


def enqueue_unmanic(path: str, *, dry_run: bool) -> str:
    if dry_run:
        return "dry-run would enqueue"
    con = sqlite3.connect(UNMANIC_DB, timeout=30)
    con.execute("pragma busy_timeout=30000")
    try:
        row = con.execute("select id, status from tasks where abspath = ?", (path,)).fetchone()
        if row:
            task_id, status = row
            if status == "in_progress":
                return f"already in progress as task {task_id}"
            con.execute(
                "update tasks set status = 'pending', priority = coalesce(priority, id) + ? where id = ?",
                (PRIORITY_BOOST, task_id),
            )
            con.commit()
            return f"reprioritized existing task {task_id}"

        cur = con.execute(
            """
            insert into tasks
              (abspath, cache_path, priority, type, library_id, status, success,
               start_time, finish_time, processed_by_worker, log)
            values (?, null, 0, 'local', ?, 'pending', null, ?, ?, null, '')
            """,
            (path, LIBRARY_ID, now_iso(), now_iso()),
        )
        task_id = cur.lastrowid
        con.execute("update tasks set priority = ? where id = ?", (task_id + PRIORITY_BOOST, task_id))
        con.commit()
        return f"enqueued task {task_id}"
    finally:
        con.close()


def process_tracked(
    api_key: str,
    state: dict[str, Any],
    active_download_ids: set[str],
    *,
    dry_run: bool,
) -> int:
    changed = 0
    for download_id, item in list(state.get("tracked", {}).items()):
        if item.get("status") in {"enqueued", "skipped"}:
            continue
        if download_id in active_download_ids:
            continue

        movie_id = item.get("movie_id")
        if not movie_id:
            movie_id = movie_id_from_history(history_for_download(api_key, download_id))
            item["movie_id"] = movie_id
        if not movie_id:
            item["last_wait_reason"] = "Radarr has not linked this download to a movie yet"
            changed += 1
            continue

        radarr_path = final_movie_path(api_key, int(movie_id))
        if not radarr_path:
            item["last_wait_reason"] = "Radarr has not recorded a final imported movie file yet"
            changed += 1
            continue

        unmanic_path = map_radarr_to_unmanic(radarr_path)
        stable, reason = file_is_stable(unmanic_path, dry_run=dry_run)
        item["radarr_path"] = radarr_path
        item["unmanic_path"] = unmanic_path
        item["last_wait_reason"] = reason
        item["last_checked"] = now_iso()
        if not stable:
            changed += 1
            continue

        result = enqueue_unmanic(unmanic_path, dry_run=dry_run)
        item["status"] = "enqueued" if not dry_run else "dry_run_ready"
        item["enqueued_at"] = now_iso()
        item["enqueue_result"] = result
        log(f"{result}: {item.get('title')} -> {unmanic_path}")
        changed += 1
    return changed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    state = load_state()
    api_key = read_api_key()
    added, active_download_ids = discover_large_queue(api_key, state)
    changed = process_tracked(api_key, state, active_download_ids, dry_run=args.dry_run)
    state["last_run"] = now_iso()
    state["last_active_radarr_queue_count"] = len(active_download_ids)
    save_state(state)
    log(
        f"tracked={len(state.get('tracked', {}))} added={added} "
        f"active_radarr_queue={len(active_download_ids)} changed={changed} dry_run={args.dry_run}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
