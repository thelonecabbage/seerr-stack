import importlib.util
import json
import os
import tempfile
import fcntl
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("rescue", Path(__file__).parents[1] / "scripts/seerr_queue_rescue.py")
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)
HASH = "a" * 40
NOW = 1800000000
START = datetime.fromtimestamp(NOW - 86400, timezone.utc).isoformat()


def torrent(**changes):
    return {"hash": HASH, "state": "stalledDL", "downloaded": 0, "progress": 0,
            "dlspeed": 0, "num_seeds": 0, "num_leechs": 0, "availability": 0,
            "last_activity": NOW - 86400, **changes}


def row(**changes):
    return {"id": 1, "movieId": 10, "downloadId": HASH, "protocol": "torrent",
            "downloadClient": "qBittorrent", "status": "warning", "sizeleft": 100,
            "added": "2020-01-01T00:00:00Z", **changes}


class RescueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.json"
        p = patch.object(r, "STATE_PATH", self.path)
        p.start()
        self.addCleanup(p.stop)
        p = patch.dict(os.environ, {}, clear=True)
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(r, "container_info", return_value={"State": {"StartedAt": START}})
        p.start()
        self.addCleanup(p.stop)

    def observation(self, t, hours):
        state = {"version": 1}
        for offset in range(0, int(hours * 3600) + 1, 1800):
            r.observe(state, {HASH: t}, NOW + offset, START)
        return state

    def candidate(self, t, state, hours=4, item=None):
        return r.is_candidate("radarr", item or row(), 4, 48, 720,
                              datetime.fromtimestamp(NOW + hours * 3600, timezone.utc), t,
                              state["torrents"][HASH])[0]

    def test_first_observation_does_not_inherit_torrent_age(self):
        t = torrent()
        self.assertFalse(self.candidate(t, self.observation(t, 0), 0))

    def test_zero_availability_requires_four_observed_hours(self):
        t = torrent()
        self.assertFalse(self.candidate(t, self.observation(t, 3), 3))
        self.assertTrue(self.candidate(t, self.observation(t, 4)))

    def test_metadata_requires_two_hours_and_no_peers(self):
        t = torrent(state="metaDL")
        self.assertTrue(self.candidate(t, self.observation(t, 2), 2))
        t["num_leechs"] = 1
        self.assertFalse(self.candidate(t, self.observation(t, 4)))

    def test_bytes_progress_protects_even_with_zero_instant_speed(self):
        t = torrent(progress=.1, downloaded=100)
        state = self.observation(t, 48)
        t["downloaded"] += 1
        r.observe(state, {HASH: t}, NOW + 48 * 3600 + 1800, START)
        self.assertFalse(self.candidate(t, state, 48.5))

    def test_slow_speed_protects_old_download(self):
        t = torrent(dlspeed=1, progress=.01)
        self.assertFalse(self.candidate(t, self.observation(t, 49), 49))

    def test_partial_stall_keeps_old_threshold(self):
        t = torrent(progress=.25, downloaded=250)
        self.assertFalse(self.candidate(t, self.observation(t, 4)))
        self.assertTrue(self.candidate(t, self.observation(t, 48), 48))

    def test_excluded_states_never_rescued(self):
        for status in ["queuedDL", "stoppedDL", "pausedDL", "checkingDL", "moving", "error", "missingFiles"]:
            with self.subTest(status=status):
                t = torrent(state=status)
                self.assertFalse(self.candidate(t, self.observation(t, 49), 49))

    def test_missing_and_negative_data_do_not_mean_zero(self):
        for field in ["num_seeds", "availability", "downloaded", "last_activity"]:
            for value in [None, -1]:
                t = torrent(**{field: value})
                self.assertFalse(self.candidate(t, self.observation(t, 49), 49))

    def test_restart_and_observation_gap_reset_timer(self):
        t = torrent()
        state = self.observation(t, 4)
        r.observe(state, {HASH: t}, NOW + 20000, START)
        self.assertEqual(state["torrents"][HASH]["since"], NOW + 20000)
        restart = datetime.fromtimestamp(NOW + 20000, timezone.utc).isoformat()
        r.observe(state, {HASH: t}, NOW + 21000, restart)
        self.assertIsNone(state["torrents"][HASH]["kind"])

    def test_completed_never_blocklisted_even_executable_warning(self):
        t = torrent(progress=1, state="stoppedUP")
        self.assertFalse(self.candidate(t, self.observation(t, 49), 49, row(status="completed", title="bad.exe")))

    def test_tracker_error_not_enough(self):
        t = torrent(state="downloading", num_seeds=1)
        self.assertFalse(self.candidate(t, self.observation(t, 49), 49, row(errorMessage="tracker timed out")))

    def test_cooldown_tracks_media_across_hashes(self):
        state = {"version": 1}
        r.reserve(state, "radarr", [10], HASH, NOW)
        r.reserve(state, "radarr", [10], "b" * 40, NOW + 1)
        self.assertTrue(r.cooldown(state, "radarr", [10], NOW + 2))
        self.assertFalse(r.cooldown(state, "radarr", [11], NOW + 2))
        self.assertFalse(r.cooldown(state, "radarr", [10], NOW + 86402))
        self.assertEqual(json.loads(self.path.read_text())["rescued"]["radarr:" + HASH]["phase"], "reserved")

    def test_grouped_rescue_deletes_once_and_searches_once(self):
        t = torrent()
        state = self.observation(t, 4)
        with patch.object(r, "qbit_torrents_by_hash", return_value={HASH: t}), patch.object(r, "request", return_value={"id": 99}) as api:
            r.rescue("radarr", [row(), row(id=2)], {HASH: t}, state, False, datetime.fromtimestamp(NOW + 14400, timezone.utc))
        self.assertEqual(api.call_count, 2)
        self.assertIn("skipRedownload=true", api.call_args_list[0].args[2])
        self.assertEqual(api.call_args_list[1].args[3]["movieIds"], [10])

    def test_reservation_prevents_retry_after_ambiguous_failure(self):
        t = torrent()
        state = self.observation(t, 4)
        now = datetime.fromtimestamp(NOW + 14400, timezone.utc)
        with patch.object(r, "qbit_torrents_by_hash", return_value={HASH: t}), patch.object(r, "request", side_effect=OSError("timeout")):
            with self.assertRaises(OSError):
                r.rescue("radarr", [row()], {HASH: t}, state, False, now)
        with patch.object(r, "request") as api:
            r.rescue("radarr", [row()], {HASH: t}, state, False, now)
            api.assert_not_called()

    def test_fresh_progress_cancels_selected_rescue(self):
        t = torrent()
        state = self.observation(t, 4)
        with patch.object(r, "qbit_torrents_by_hash", return_value={HASH: torrent(downloaded=1)}), patch.object(r, "request") as api:
            r.rescue("radarr", [row()], {HASH: t}, state, False, datetime.fromtimestamp(NOW + 14400, timezone.utc))
            api.assert_not_called()

    def test_dry_run_has_no_api_writes_or_state_file(self):
        t = torrent()
        state = self.observation(t, 4)
        with patch.object(r, "request") as api:
            r.rescue("radarr", [row()], {HASH: t}, state, True, datetime.fromtimestamp(NOW + 14400, timezone.utc))
            api.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_history_requires_every_episode_and_current_file(self):
        history = [{"episodeId": 1, "data": {"fileId": "5", "importedPath": "/tv/show/one.mkv"}}]
        with patch.object(r, "import_history", return_value=history), patch.object(r, "check_path"), patch.object(r, "request", side_effect=[{"episodeFileId": 5}, {"path": "/tv/show/one.mkv"}]):
            self.assertFalse(r.verified_imports("sonarr", HASH, [row(episodeId=1), row(episodeId=2)], {}))

    def test_incomplete_queue_snapshot_fails_closed(self):
        with patch.object(r, "request", return_value={"totalRecords": 2, "records": [row()]}):
            with self.assertRaises(RuntimeError):
                r.queue_records("radarr")

    def test_cleanup_preserves_files_and_disables_redownload(self):
        t = torrent(progress=1, state="stoppedUP")
        with patch.dict(os.environ, {"SEERR_RESCUE_CLEANUP_ENABLED": "1"}), patch.object(r, "container_info", return_value={"Id": "test"}), patch.object(r, "verified_imports", return_value=True), patch.object(r, "verified_payload", return_value=True), patch.object(r, "qbit_request") as qbit, patch.object(r, "request") as api:
            r.completed("radarr", [row(status="completed")], {HASH: t}, {}, False, NOW)
        self.assertEqual(qbit.call_args.args[1]["deleteFiles"], "false")
        self.assertIn("removeFromClient=false", api.call_args.args[2])
        self.assertIn("blocklist=false", api.call_args.args[2])
        self.assertIn("skipRedownload=true", api.call_args.args[2])

    def test_import_check_failure_preserves_download(self):
        t = torrent(progress=1, state="stoppedUP")
        state = {"imports": {"radarr:" + HASH: {"first_seen": NOW - 7200, "attempts": 0}}}
        with patch.dict(os.environ, {"SEERR_RESCUE_IMPORT_ENABLED": "1"}), patch.object(r, "container_info", return_value={"Id": "test"}), patch.object(r, "verified_imports", return_value=False), patch.object(r, "request", return_value=[]) as api, patch.object(r, "check_path", side_effect=RuntimeError("mount missing")), patch.object(r, "qbit_request") as qbit:
            r.completed("radarr", [row(status="completed", outputPath="/downloads/file")], {HASH: t}, state, False, NOW)
        qbit.assert_not_called()
        self.assertTrue(all(c.args[1] == "GET" for c in api.call_args_list))
        self.assertEqual(state["imports"]["radarr:" + HASH]["attempts"], 0)

    def test_import_retry_is_scoped_copy_and_limited(self):
        t = torrent(progress=1, state="stoppedUP", save_path="/downloads")
        state = {"imports": {"radarr:" + HASH: {"first_seen": NOW - 7200, "attempts": 0}}}
        item = row(status="completed", outputPath="/downloads/movie.mkv")
        def api(app, method, path, body=None):
            if path == "/api/v3/command":
                return [] if method == "GET" else {"id": 77}
            return {"path": "/movies/Movie"}
        with patch.dict(os.environ, {"SEERR_RESCUE_IMPORT_ENABLED": "1"}), patch.object(r, "container_info", return_value={"Id": "test"}), patch.object(r, "verified_imports", return_value=False), patch.object(r, "check_path"), patch.object(r, "qbit_request", return_value=[{"name": "movie.mkv", "priority": 1, "progress": 1}]), patch.object(r, "request", side_effect=api) as calls:
            r.completed("radarr", [item], {HASH: t}, state, False, NOW)
            posts = [c for c in calls.call_args_list if c.args[1] == "POST"]
            self.assertEqual(len(posts), 1)
            self.assertEqual(posts[0].args[3], {"name": "DownloadedMoviesScan", "path": "/downloads/movie.mkv", "downloadClientId": HASH.upper(), "importMode": "Copy"})
            calls.reset_mock()
            r.completed("radarr", [item], {HASH: t}, state, False, NOW + 1800)
            calls.assert_not_called()
            state["imports"]["radarr:" + HASH]["attempts"] = 2
            r.completed("radarr", [item], {HASH: t}, state, False, NOW + 86400)
            calls.assert_not_called()

    def test_missing_payload_import_blocks_cleanup(self):
        with patch.object(r, "qbit_request", return_value=[{"name": "extra.mkv", "priority": 1, "progress": 1}]), patch.object(r, "import_history", return_value=[]):
            self.assertFalse(r.verified_payload("radarr", HASH, torrent(save_path="/downloads"), [row()]))

    def test_invalid_and_executable_files_block_import(self):
        for name in ["../movie.mkv", "/movie.mkv", "movie.exe"]:
            with self.assertRaises(RuntimeError):
                r.video_paths(torrent(save_path="/downloads"), [{"name": name, "priority": 1, "progress": 1}])

    def test_metadata_peer_arrival_cancels_rescue(self):
        t = torrent(state="metaDL")
        state = self.observation(t, 2)
        with patch.object(r, "qbit_torrents_by_hash", return_value={HASH: torrent(state="metaDL", num_leechs=1)}), patch.object(r, "request") as api:
            r.rescue("radarr", [row()], {HASH: t}, state, False, datetime.fromtimestamp(NOW + 7200, timezone.utc))
            api.assert_not_called()

    def test_per_run_cap_and_dry_run_cooldowns(self):
        torrents = {str(i) * 40: torrent(hash=str(i) * 40) for i in range(1, 6)}
        state = {"version": 1}
        for offset in range(0, 14401, 1800):
            r.observe(state, torrents, NOW + offset, START)
        rows = [row(id=i, movieId=i, downloadId=str(i) * 40) for i in range(1, 6)]
        with patch.object(r, "request") as api:
            r.rescue("radarr", rows, torrents, state, True, datetime.fromtimestamp(NOW + 14400, timezone.utc))
            api.assert_not_called()
        self.assertEqual(len(state["attempts"]), 3)

    def test_lock_skips_all_application_calls(self):
        with self.path.with_suffix(".lock").open("a") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch("sys.argv", ["rescue"]), patch.object(r, "qbit_torrents_by_hash") as api:
                self.assertEqual(r.main(), 0)
                api.assert_not_called()

    def test_corrupt_state_stops_before_application_writes(self):
        self.path.write_text("not json")
        with patch("sys.argv", ["rescue"]), patch.object(r, "qbit_torrents_by_hash") as api:
            self.assertEqual(r.main(), 1)
            api.assert_not_called()

    def test_observe_persists_observations_without_simulated_attempts(self):
        with patch("sys.argv", ["rescue", "--observe", "--no-promote"]), patch.object(r, "qbit_torrents_by_hash", return_value={HASH: torrent()}), patch.object(r, "queue_records", return_value=[row()]), patch.object(r, "network_diagnostics"), patch.object(r, "ensure_qbit_preferences", return_value=[]), patch.object(r, "completed"), patch.object(r, "request") as api:
            self.assertEqual(r.main(), 0)
            api.assert_not_called()
        state = json.loads(self.path.read_text())
        self.assertIn(HASH, state["torrents"])
        self.assertNotIn("attempts", state)


if __name__ == "__main__":
    unittest.main()
