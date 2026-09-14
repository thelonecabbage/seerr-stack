#!/usr/bin/env python3
"""Enable qBittorrent alternative speed limits while Plex has active sessions."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_DIR = Path(os.environ.get("PLEX_QBIT_CONFIG_DIR", "/srv/media-stack/config"))
DEFAULT_PLEX_PREFS = Path(
    os.environ.get(
        "PLEX_QBIT_PLEX_PREFS",
        "/srv/plex/config/Library/Application Support/Plex Media Server/Preferences.xml",
    )
)
DEFAULT_QBIT_CONF = DEFAULT_CONFIG_DIR / "qbittorrent/qBittorrent/qBittorrent.conf"
DEFAULT_STATE_FILE = Path("/var/lib/plex-qbit-speed-guard/state.json")
DEFAULT_LOCK_FILE = Path("/run/plex-qbit-speed-guard.lock")


class GuardError(RuntimeError):
    pass


def log(message: str) -> None:
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {message}", flush=True)


def env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value in (None, ""):
        return default
    return int(value)


def read_plex_token(path: Path) -> str:
    token = ET.parse(path).getroot().attrib.get("PlexOnlineToken")
    if not token:
        raise GuardError(f"missing PlexOnlineToken in {path}")
    return token


def read_qbit_api_key(path: Path) -> str:
    for line in path.read_text().splitlines():
        if line.startswith("WebUI\\APIKey="):
            key = line.split("=", 1)[1].strip()
            if key:
                return key
    raise GuardError(f"missing WebUI API key in {path}")


def plex_session_count(base_url: str, token: str, timeout: int) -> int:
    req = urllib.request.Request(
        base_url.rstrip("/") + "/status/sessions",
        headers={"X-Plex-Token": token},
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        root = ET.fromstring(response.read())

    count = 0
    for child in root:
        if child.tag not in {"Video", "Track", "Photo"}:
            continue
        player = child.find("Player")
        state = (player.attrib.get("state") if player is not None else "") or ""
        if state.lower() != "stopped":
            count += 1
    if count == 0:
        return int(root.attrib.get("size", "0") or "0")
    return count


def qbit_request(
    base_url: str,
    api_key: str,
    path: str,
    timeout: int,
    params: dict[str, Any] | None = None,
    method: str = "GET",
) -> bytes:
    url = base_url.rstrip("/") + path
    data = None
    headers = {"Authorization": f"Bearer {api_key}"}
    if params is not None:
        encoded = urllib.parse.urlencode(params, doseq=True).encode()
        if method == "GET":
            url += "?" + encoded.decode()
        else:
            data = encoded
            headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read()


def qbit_speed_mode(base_url: str, api_key: str, timeout: int) -> bool:
    payload = qbit_request(base_url, api_key, "/api/v2/transfer/speedLimitsMode", timeout)
    return payload.decode().strip() == "1"


def qbit_preferences(base_url: str, api_key: str, timeout: int) -> dict[str, Any]:
    payload = qbit_request(base_url, api_key, "/api/v2/app/preferences", timeout)
    prefs = json.loads(payload.decode())
    if not isinstance(prefs, dict):
        raise GuardError("qBittorrent preferences response was not an object")
    return prefs


def qbit_set_preferences(
    base_url: str,
    api_key: str,
    timeout: int,
    changes: dict[str, Any],
    dry_run: bool,
) -> None:
    if dry_run or not changes:
        return
    qbit_request(
        base_url,
        api_key,
        "/api/v2/app/setPreferences",
        timeout,
        params={"json": json.dumps(changes, separators=(",", ":"))},
        method="POST",
    )


def qbit_toggle_speed_mode(base_url: str, api_key: str, timeout: int, dry_run: bool) -> None:
    if dry_run:
        return
    qbit_request(
        base_url,
        api_key,
        "/api/v2/transfer/toggleSpeedLimitsMode",
        timeout,
        method="POST",
    )


def load_state(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        return {}


def save_state(path: Path, state: dict[str, Any], dry_run: bool) -> None:
    if dry_run:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, sort_keys=True) + "\n")
    tmp.replace(path)


def desired_alt_mode(active_sessions: int, state: dict[str, Any], now: int, idle_grace_seconds: int) -> bool:
    if active_sessions > 0:
        state["last_active_at"] = now
        return True
    last_active_at = int(state.get("last_active_at") or 0)
    return bool(last_active_at and now - last_active_at < idle_grace_seconds)


def run(args: argparse.Namespace) -> int:
    timeout = args.timeout
    plex_token = read_plex_token(args.plex_prefs)
    qbit_api_key = read_qbit_api_key(args.qbit_conf)

    active_sessions = plex_session_count(args.plex_url, plex_token, timeout)
    prefs = qbit_preferences(args.qbit_url, qbit_api_key, timeout)
    current_alt = qbit_speed_mode(args.qbit_url, qbit_api_key, timeout)

    now = int(time.time())
    state = load_state(args.state_file)
    target_alt = desired_alt_mode(active_sessions, state, now, args.idle_grace_seconds)

    changes: dict[str, Any] = {}
    if args.alt_dl_limit_bytes >= 0 and prefs.get("alt_dl_limit") != args.alt_dl_limit_bytes:
        changes["alt_dl_limit"] = args.alt_dl_limit_bytes
    if args.alt_up_limit_bytes >= 0 and prefs.get("alt_up_limit") != args.alt_up_limit_bytes:
        changes["alt_up_limit"] = args.alt_up_limit_bytes

    action_parts: list[str] = []
    if changes:
        qbit_set_preferences(args.qbit_url, qbit_api_key, timeout, changes, args.dry_run)
        action_parts.append("set_prefs=" + ",".join(sorted(changes)))

    if current_alt != target_alt:
        qbit_toggle_speed_mode(args.qbit_url, qbit_api_key, timeout, args.dry_run)
        action_parts.append("enable_alt" if target_alt else "disable_alt")

    state.update(
        {
            "last_run_at": now,
            "last_session_count": active_sessions,
            "last_target_alt": target_alt,
        }
    )
    save_state(args.state_file, state, args.dry_run)

    action = "+".join(action_parts) if action_parts else "none"
    log(
        "plex_sessions={sessions} qbit_alt_current={current} qbit_alt_target={target} "
        "alt_dl_limit={alt_dl} alt_up_limit={alt_up} dry_run={dry_run} action={action}".format(
            sessions=active_sessions,
            current=int(current_alt),
            target=int(target_alt),
            alt_dl=changes.get("alt_dl_limit", prefs.get("alt_dl_limit")),
            alt_up=changes.get("alt_up_limit", prefs.get("alt_up_limit")),
            dry_run=int(args.dry_run),
            action=action,
        )
    )
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plex-url", default=os.environ.get("PLEX_QBIT_PLEX_URL", "http://127.0.0.1:32400"))
    parser.add_argument("--qbit-url", default=os.environ.get("PLEX_QBIT_QBIT_URL", "http://127.0.0.1:8081"))
    parser.add_argument("--plex-prefs", type=Path, default=Path(os.environ.get("PLEX_QBIT_PLEX_PREFS", DEFAULT_PLEX_PREFS)))
    parser.add_argument("--qbit-conf", type=Path, default=Path(os.environ.get("PLEX_QBIT_QBIT_CONF", DEFAULT_QBIT_CONF)))
    parser.add_argument("--state-file", type=Path, default=Path(os.environ.get("PLEX_QBIT_STATE_FILE", DEFAULT_STATE_FILE)))
    parser.add_argument("--lock-file", type=Path, default=Path(os.environ.get("PLEX_QBIT_LOCK_FILE", DEFAULT_LOCK_FILE)))
    parser.add_argument("--idle-grace-seconds", type=int, default=env_int("PLEX_QBIT_IDLE_GRACE_SECONDS", 120))
    parser.add_argument("--alt-dl-limit-bytes", type=int, default=env_int("PLEX_QBIT_ALT_DL_LIMIT_BYTES", 2 * 1024 * 1024))
    parser.add_argument("--alt-up-limit-bytes", type=int, default=env_int("PLEX_QBIT_ALT_UP_LIMIT_BYTES", -1))
    parser.add_argument("--timeout", type=int, default=env_int("PLEX_QBIT_TIMEOUT_SECONDS", 10))
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    args.lock_file.parent.mkdir(parents=True, exist_ok=True)
    with args.lock_file.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("action=skip reason=already_running")
            return 0
        try:
            return run(args)
        except Exception as exc:
            log(f"action=error error={type(exc).__name__}: {exc}")
            return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
