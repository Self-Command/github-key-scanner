"""Repository push-time contracts, using only offline synthetic fixtures."""
import argparse
import contextlib
import importlib
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import config
from gks.github_search import GitHubCodeSearch, Hit
from gks.privacy import MonitoringDatabase

NOW = datetime(2026, 10, 13, 4, 0, tzinfo=timezone.utc)
KEY = "sk-" + "a" * 32


def response(data, status=200):
    result = mock.Mock(status_code=status)
    result.json.return_value = data
    return result


class WindowTests(unittest.TestCase):
    def test_presets_are_rolling_and_default_is_30_days(self):
        from gks.scan_scope import resolve_window, load_settings
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(load_settings(Path(td) / "settings.json")["preset"], "30d")
        expected = {"24h": "2026-10-12T04:00:00+00:00", "7d": "2026-10-06T04:00:00+00:00",
                    "30d": "2026-09-13T04:00:00+00:00"}
        for preset, start in expected.items():
            window = resolve_window({"preset": preset}, now=NOW)
            self.assertEqual(window.start.isoformat(), start)
            self.assertEqual(window.end, NOW)
            self.assertTrue(window.contains(window.start))
            self.assertFalse(window.contains(window.end))

    def test_custom_dates_include_whole_beijing_end_date(self):
        from gks.scan_scope import resolve_window
        window = resolve_window({"preset": "custom", "start_date": "2026-10-01", "end_date": "2026-10-02"}, now=NOW)
        self.assertEqual(window.start.isoformat(), "2026-09-30T16:00:00+00:00")
        self.assertEqual(window.end.isoformat(), "2026-10-02T16:00:00+00:00")
        self.assertTrue(window.contains(datetime(2026, 10, 2, 15, 59, 59, tzinfo=timezone.utc)))
        self.assertFalse(window.contains(window.end))

    def test_invalid_settings_rejected_without_replacing_saved_value(self):
        from gks.scan_scope import save_settings, load_settings
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "scope.json"
            save_settings(path, {"preset": "7d"}, now=NOW)
            for invalid in ([], {"preset": []}, {"preset": {}}, {"preset": "all"}, {"preset": "custom", "start_date": "2026-10-14", "end_date": "2026-10-15"},
                            {"preset": "custom", "start_date": "0001-01-01", "end_date": "2026-10-13"},
                            {"preset": "custom", "start_date": "2026-10-02", "end_date": "2026-10-01"},
                            {"preset": "custom", "start_date": "bad", "end_date": "2026-10-01"}):
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    save_settings(path, invalid, now=NOW)
                self.assertEqual(load_settings(path)["preset"], "7d")
            path.write_text('{"preset":', encoding="utf-8")
            with self.assertRaises(ValueError):
                load_settings(path)


class RepositoryFilterTests(unittest.TestCase):
    def test_metadata_cached_time_filtered_public_only_and_newest_first(self):
        from gks.scan_scope import resolve_window
        window = resolve_window({"preset": "30d"}, now=NOW)
        hits = [Hit(repo="example/" + name, path=path, html_url="", language="Python")
                for name, path in [("older", "a.py"), ("newer", "a.py"), ("older", "b.py"),
                                   ("outside", "a.py"), ("private", "a.py"), ("missing", "a.py")]]
        metadata = {
            "example/older": {"private": False, "pushed_at": "2026-10-01T04:00:00Z"},
            "example/newer": {"private": False, "pushed_at": "2026-10-12T04:00:00Z"},
            "example/outside": {"private": False, "pushed_at": "2026-01-01T00:00:00Z"},
            "example/private": {"private": True, "pushed_at": "2026-10-12T04:00:00Z"},
            "example/missing": {"private": False, "pushed_at": None},
        }
        searcher = GitHubCodeSearch("synthetic-token", min_interval=0)
        with mock.patch.object(searcher.session, "get", side_effect=lambda url, **kw: response(metadata[url.split('/repos/')[1]])) as get:
            accepted, stats = searcher.filter_recent_repositories(hits, window)
        self.assertEqual([h.repo for h in accepted], ["example/newer", "example/older", "example/older"])
        self.assertEqual(get.call_count, 5)
        self.assertEqual(stats, {"repos_checked": 5, "files_outside_time": 1, "files_unverified_time": 2})
        self.assertEqual(accepted[0].repo_pushed_at, "2026-10-12T04:00:00+00:00")

    def test_metadata_errors_fail_closed_and_are_counted(self):
        from gks.scan_scope import resolve_window
        searcher = GitHubCodeSearch("synthetic-token", min_interval=0)
        hits = [Hit(repo="example/" + name, path="a.py", html_url="", language="") for name in ("timeout", "limit", "bad")]
        with mock.patch.object(searcher.session, "get", side_effect=[
            __import__('requests').Timeout("synthetic"), response({}, 403), response({"private": False, "pushed_at": "bad"})]):
            accepted, stats = searcher.filter_recent_repositories(hits, resolve_window({"preset": "30d"}, now=NOW))
        self.assertEqual(accepted, [])
        self.assertEqual(stats["files_unverified_time"], 3)


class SettingsApiTests(unittest.TestCase):
    def test_settings_api_persists_and_rejects_bad_ranges(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.object(config, "DB_PATH", Path(td) / "monitor.db"), \
                mock.patch.object(config, "SCAN_SETTINGS_FILE", Path(td) / "scope.json", create=True):
            web = importlib.reload(importlib.import_module("web.app"))
            client = web.app.test_client()
            self.assertEqual(client.get("/api/scan/settings").json["settings"]["preset"], "30d")
            saved = client.put("/api/scan/settings", json={"preset": "24h"})
            self.assertEqual(saved.status_code, 200)
            self.assertEqual(json.loads(config.SCAN_SETTINGS_FILE.read_text())["preset"], "24h")
            self.assertEqual(client.get("/api/status").json["scan_scope"]["settings"]["preset"], "24h")
            self.assertEqual(client.put("/api/scan/settings", json={"preset": "custom", "start_date": "bad", "end_date": "bad"}).status_code, 400)
            self.assertEqual(client.put("/api/scan/settings", json=[]).status_code, 400)
            self.assertEqual(client.get("/api/scan/settings").json["settings"]["preset"], "24h")


class PipelineTests(unittest.TestCase):
    def test_failed_or_incomplete_search_is_not_reported_as_success(self):
        import main
        from gks.scan_scope import save_settings
        args = argparse.Namespace(max_pages=1, demo=False, ai_mode="rules", incremental=True,
                                  since="", full=False, extensions=["py"])
        for status, data in [(503, {}), (200, {"items": [], "total_count": 0, "incomplete_results": True})]:
            with self.subTest(status=status), tempfile.TemporaryDirectory() as td, \
                    mock.patch.object(config, "SCAN_SETTINGS_FILE", Path(td) / "scope.json"), \
                    mock.patch.object(config, "GITHUB_TOKEN", "synthetic-token"):
                save_settings(config.SCAN_SETTINGS_FILE, {"preset": "30d"})
                db = MonitoringDatabase(Path(td) / "monitor.db")
                r = response(data, status)
                r.headers = {}
                r.text = "synthetic failure"
                with mock.patch("gks.github_search.requests.Session.get", return_value=r), contextlib.redirect_stdout(io.StringIO()):
                    result = main.do_scan(args, db)
                self.assertFalse(result["ok"])
                task = db.recent_tasks(1)[0]
                self.assertEqual(task["status"], "error")
                self.assertTrue(task["error"])
                self.assertEqual(db.get_cursor()["last_window_to"], "")

    def test_scope_gate_runs_before_content_fetch_and_task_records_actual_scope(self):
        import main
        from gks.scan_scope import save_settings
        old = Hit(repo="example/old", path="a.py", html_url="", language="Python", fragments=[KEY])
        new = Hit(repo="example/new", path="a.py", html_url="", language="Python", fragments=[KEY])
        args = argparse.Namespace(max_pages=1, demo=False, ai_mode="rules", incremental=True,
                                  since="", full=False, extensions=["py"])
        with tempfile.TemporaryDirectory() as td, mock.patch.object(config, "SCAN_SETTINGS_FILE", Path(td) / "scope.json", create=True), \
                mock.patch.object(config, "GITHUB_TOKEN", "synthetic-token"), mock.patch.object(config, "FETCH_FILE_CONTENT", True):
            save_settings(config.SCAN_SETTINGS_FILE, {"preset": "30d"})
            db = MonitoringDatabase(Path(td) / "monitor.db")
            searcher = mock.Mock()
            searcher.search_errors = 0
            searcher.search_incomplete_queries = 0
            searcher.build_queries.return_value = [("sk- language:python", 1)]
            searcher.search.return_value = [old, new]
            new.repo_pushed_at = datetime.now(timezone.utc).isoformat()
            searcher.filter_recent_repositories.return_value = ([new], {"repos_checked": 2, "files_outside_time": 1, "files_unverified_time": 0})
            searcher.fetch_full_text.return_value = KEY
            stream = io.StringIO()
            with mock.patch("main.GitHubCodeSearch", return_value=searcher), contextlib.redirect_stdout(stream):
                result = main.do_scan(args, db)
            self.assertTrue(result["ok"], stream.getvalue())
            searcher.fetch_full_text.assert_called_once_with(new)
            task = db.recent_tasks(1)[0]
            self.assertEqual(task["repo_time_preset"], "30d")
            self.assertTrue(task["repo_pushed_from"])
            self.assertTrue(task["repo_pushed_before"])
            self.assertEqual(task["files_outside_time"], 1)
            self.assertEqual(task["files_analyzed"], 1)

    def test_same_path_new_blob_is_analyzed_and_unverified_scope_not_successful(self):
        import main
        from gks.scan_scope import save_settings
        args = argparse.Namespace(max_pages=1, demo=False, ai_mode="rules", incremental=True,
                                  since="", full=False, extensions=["py"])
        with tempfile.TemporaryDirectory() as td, mock.patch.object(config, "SCAN_SETTINGS_FILE", Path(td) / "scope.json", create=True), \
                mock.patch.object(config, "GITHUB_TOKEN", "synthetic-token"), mock.patch.object(config, "FETCH_FILE_CONTENT", False):
            save_settings(config.SCAN_SETTINGS_FILE, {"preset": "30d"})
            db = MonitoringDatabase(Path(td) / "monitor.db")
            searcher = mock.Mock()
            searcher.search_errors = 0
            searcher.search_incomplete_queries = 0
            searcher.build_queries.return_value = [("sk- language:python", 1)]
            for sha, unverified in [("a" * 40, 0), ("a" * 40, 0), ("b" * 40, 1)]:
                hit = Hit(repo="example/new", path="a.py", html_url="", language="Python", fragments=[KEY], content_sha=sha)
                searcher.search.return_value = [hit]
                searcher.filter_recent_repositories.return_value = ([hit], {"repos_checked": 1, "files_outside_time": 0, "files_unverified_time": unverified})
                with mock.patch("main.GitHubCodeSearch", return_value=searcher), contextlib.redirect_stdout(io.StringIO()):
                    result = main.do_scan(args, db)
                self.assertEqual(result["analyzed"], 0 if db.recent_tasks(1)[0]["id"] == 2 else 1)
                self.assertEqual(result["ok"], unverified == 0)
            task = db.recent_tasks(1)[0]
            self.assertEqual(task["status"], "error")
            self.assertIn("不完整", task["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
