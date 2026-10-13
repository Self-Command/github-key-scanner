"""Deployment privacy contracts; fixtures are synthetic and never probe providers."""
import argparse
import contextlib
import hashlib
import importlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main
from config import config
from gks.analyzer import AIAnalyzer
from gks.github_search import Hit
from gks.github_search import GitHubCodeSearch
from gks.privacy import key_identity, redact_text

KEY = "sk-" + "abcdefgh" + "a" * 20 + "1234"
OTHER_KEY = "sk-" + "abcdefgh" + "b" * 20 + "1234"


def record(key=KEY):
    return {
        "api_key": key, "provider": "DeepSeek", "api_base": "https://api.deepseek.com/v1",
        "confidence": 0.8, "category": "llm", "source_url": "https://github.com/example/demo/blob/main/config.py",
        "repo": "example/demo", "file_path": "config.py", "language": "Python",
        "context": f'API_KEY="{key}"\nOTHER_KEY="{OTHER_KEY}"',
        "notes": f"Synthetic candidate {key}; another candidate {OTHER_KEY}",
    }


class PrivacyTests(unittest.TestCase):
    def test_secret_filename_is_not_logged_or_used_as_database_identity(self):
        hit = Hit(repo="example/demo", path=KEY + "suffix.env", html_url=f"https://example.test/{KEY}",
                  language="Python", full_text=f'API_KEY="{KEY}"')
        args = argparse.Namespace(max_pages=1, demo=True, ai_mode="rules", incremental=True,
                                  since="", full=False, extensions=None)
        with tempfile.TemporaryDirectory() as td:
            db = main.KeyDatabase(Path(td) / "monitor.db")
            stream = io.StringIO()
            with mock.patch("main.load_sample_hits", return_value=[hit]), contextlib.redirect_stdout(stream):
                self.assertTrue(main.do_scan(args, db)["ok"])
            self.assertNotIn(KEY, stream.getvalue())
            self.assertFalse(db.should_scan_file(f"{hit.repo}/{hit.path}"))
            for f in Path(td).iterdir():
                self.assertNotIn(KEY.encode(), f.read_bytes())

    def test_generic_credentials_do_not_reach_exports_or_ai(self):
        secret = "synthetic-secret-" + "Z" * 30
        text = (f'DEEPSEEK_API_KEY="{KEY}"\nAWS_SECRET_ACCESS_KEY="{secret}"\n'
                f'DEEPSEEK_BASE_URL="https://user:{secret}@api.deepseek.com/v1?api_key={secret}"')
        hit = Hit(repo="example/demo", path="config.py", html_url=record()["source_url"],
                  language="Python", full_text=text)
        response = mock.Mock(status_code=200)
        response.json.return_value = {"choices": [{"message": {"content": '{"keys": []}'}}]}
        with mock.patch("gks.analyzer.requests.post", return_value=response) as post:
            AIAnalyzer("https://example.test/v1", "synthetic-own-ai-key", "fixture").analyze(hit)
        outbound = json.dumps(post.call_args.kwargs["json"])
        self.assertNotIn(secret, outbound)
        self.assertIn("KEY_CANDIDATE_1", outbound)
        with tempfile.TemporaryDirectory() as td:
            db = main.KeyDatabase(Path(td) / "monitor.db")
            from gks.analyzer import RuleAnalyzer
            for rec in RuleAnalyzer().analyze(hit):
                db.upsert({**record(), **rec, "context": text})
            self.assertNotIn(secret, json.dumps(db.query()) + db.export_csv())
            for f in Path(td).iterdir():
                self.assertNotIn(secret.encode(), f.read_bytes())

    def test_endpoint_paths_and_free_form_notes_are_not_persisted_or_logged(self):
        secret = "synthetic-gateway-credential-" + "Q" * 32
        hit = Hit(repo="example/demo", path="config.py", html_url=record()["source_url"],
                  language="Python", full_text=(f'DEEPSEEK_API_KEY="{KEY}"\n'
                      f'DEEPSEEK_BASE_URL="https://api.deepseek.com/v1/{secret}"'))
        args = argparse.Namespace(max_pages=1, demo=True, ai_mode="rules", incremental=True,
                                  since="", full=False, extensions=None)
        with tempfile.TemporaryDirectory() as td:
            db = main.KeyDatabase(Path(td) / "monitor.db")
            stream = io.StringIO()
            with mock.patch("main.load_sample_hits", return_value=[hit]), contextlib.redirect_stdout(stream):
                self.assertTrue(main.do_scan(args, db)["ok"])
            self.assertNotIn(secret, stream.getvalue())
            rows, _ = db.query()
            self.assertEqual(rows[0]["api_base_url"], "https://api.deepseek.com/v1")
            db.upsert({**record(OTHER_KEY), "notes": "unlabeled " + secret})
            self.assertNotIn(secret, json.dumps(db.query()) + db.export_csv())
            for f in Path(td).iterdir():
                self.assertNotIn(secret.encode(), f.read_bytes())

    def test_percent_encoded_credentials_are_removed_in_original_form(self):
        full = "".join(f"%{ord(c):02X}" for c in KEY)
        partial = KEY.replace("s", "%73", 1)
        nested = full.replace("%", "%25")
        for encoded in (full, full.lower(), partial, nested):
            with self.subTest(encoded=encoded[:20]):
                cleaned = redact_text(f"source/{encoded}.env and https://example.test/{encoded}")
                self.assertNotIn(encoded, cleaned)
                self.assertIn("[REDACTED]", cleaned)

    def test_ai_context_is_a_static_signal_allowlist(self):
        secret = "synthetic-unprefixed-" + "M" * 35
        variants = [f'os.environ["AWS_SECRET_ACCESS_KEY"] = "{secret}"',
                    f'config["password"] = "{secret}"',
                    f'AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY", "{secret}")',
                    f'password: |-\n  {secret}', f'# arbitrary unlabeled value: {secret}',
                    f'url="https://api.deepseek.com/{secret}"']
        response = mock.Mock(status_code=200)
        response.json.return_value = {"choices": [{"message": {"content": '{"keys": []}'}}]}
        for variant in variants:
            with self.subTest(variant=variant[:20]):
                hit = Hit(repo=secret, path=secret, html_url=f"https://example.test/{secret}",
                          language=secret, full_text=f'DEEPSEEK_API_KEY="{KEY}"\n{variant}')
                with mock.patch("gks.analyzer.requests.post", return_value=response) as post:
                    AIAnalyzer("https://example.test/v1", "synthetic-own-ai-key", "fixture").analyze(hit)
                outbound = json.dumps(post.call_args.kwargs["json"])
                self.assertNotIn(secret, outbound)
                self.assertNotIn(KEY, outbound)
                self.assertIn("KEY_CANDIDATE_1", outbound)

    def test_private_and_unknown_visibility_hits_are_rejected(self):
        response = mock.Mock(status_code=200)
        response.json.return_value = {"total_count": 3, "items": [
            {"repository": {"full_name": "example/public", "private": False}, "path": "config.py"},
            {"repository": {"full_name": "example/private", "private": True}, "path": "config.py"},
            {"repository": {"full_name": "example/unknown"}, "path": "config.py"}]}
        searcher = GitHubCodeSearch("synthetic-token", min_interval=0, max_pages=1)
        with mock.patch.object(searcher.session, "get", return_value=response) as get:
            hits = searcher.search("sk-")
        self.assertEqual([hit.repo for hit in hits], ["example/public"])
        self.assertEqual(get.call_args.kwargs["params"]["q"], "sk-")

    def test_legacy_identity_rows_and_deleted_plaintext_are_rejected(self):
        from gks.database import KeyDatabase
        for deleted in (False, True):
            with self.subTest(deleted=deleted), tempfile.TemporaryDirectory() as td:
                path = Path(td) / "legacy.db"
                raw_db = KeyDatabase(path)
                raw_db.upsert(record(KEY if deleted else key_identity(KEY)))
                if deleted:
                    with raw_db._connect() as conn:
                        conn.execute("DELETE FROM api_keys")
                with self.assertRaisesRegex(RuntimeError, "Legacy"):
                    main.KeyDatabase(path)

    def test_database_and_csv_do_not_contain_full_keys(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "monitor.db"
            db = main.KeyDatabase(path)
            self.assertTrue(db.upsert(record()))
            rows, count = db.query()
            payload = json.dumps(rows) + db.export_csv()
            self.assertNotIn(KEY, payload)
            self.assertNotIn(OTHER_KEY, payload)
            self.assertEqual(count, 1)
            self.assertEqual(rows[0]["context"], "")
            self.assertEqual(rows[0]["key_fingerprint"], hashlib.sha256(KEY.encode()).hexdigest())
            self.assertIn("…", rows[0]["key_mask"])
            self.assertEqual(rows[0]["source_url"], record()["source_url"])
            for f in Path(td).iterdir():
                self.assertNotIn(KEY.encode(), f.read_bytes())
                self.assertNotIn(OTHER_KEY.encode(), f.read_bytes())

    def test_fingerprint_dedup_handles_identical_masks(self):
        with tempfile.TemporaryDirectory() as td:
            db = main.KeyDatabase(Path(td) / "monitor.db")
            self.assertTrue(db.upsert(record(KEY)))
            self.assertFalse(db.upsert(record(KEY)))
            self.assertTrue(db.upsert(record(OTHER_KEY)))
            rows, count = db.query()
            self.assertEqual(count, 2)
            self.assertEqual(rows[0]["key_mask"], rows[1]["key_mask"])
            self.assertNotEqual(rows[0]["key_fingerprint"], rows[1]["key_fingerprint"])
            self.assertIsNotNone(db.find_key_id(KEY))

    def test_legacy_plaintext_database_is_not_served(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "legacy.db"
            from gks.database import KeyDatabase
            raw_db = KeyDatabase(path)
            raw_db.upsert(record())
            with self.assertRaisesRegex(RuntimeError, "plaintext"):
                main.KeyDatabase(path)

    def test_scan_logs_are_redacted_and_auto_test_cannot_be_enabled(self):
        hit = Hit(repo="example/demo", path="config.py", html_url=record()["source_url"],
                  language="Python", full_text=f'DEEPSEEK_BASE_URL="https://api.deepseek.com"\nAPI_KEY="{KEY}"')
        args = argparse.Namespace(max_pages=1, demo=True, ai_mode="rules", incremental=True,
                                  since="", full=False, extensions=None)
        with tempfile.TemporaryDirectory() as td:
            db = main.KeyDatabase(Path(td) / "monitor.db")
            stream = io.StringIO()
            with mock.patch("main.load_sample_hits", return_value=[hit]), \
                    mock.patch.object(config, "AUTO_TEST_LLM", True), \
                    mock.patch("gks.key_tester.test_key", side_effect=AssertionError("Unexpected credential probe")) as probe, \
                    contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
                result = main.do_scan(args, db)
            self.assertTrue(result["ok"], stream.getvalue())
            self.assertNotIn(KEY, stream.getvalue())
            self.assertIn("sha256:", stream.getvalue())
            probe.assert_not_called()

    def test_manual_test_route_is_disabled_without_provider_requests(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.object(config, "DB_PATH", Path(td) / "web.db"):
            web = importlib.import_module("web.app")
            web = importlib.reload(web)
            web.db.upsert(record())
            key_id = web.db.query()[0][0]["id"]
            with mock.patch("gks.key_tester.requests.get", side_effect=AssertionError("Unexpected credential probe")) as probe:
                response = web.app.test_client().post(f"/api/keys/{key_id}/test")
            self.assertEqual(response.status_code, 403)
            probe.assert_not_called()
            self.assertNotIn(KEY, web.app.test_client().get("/api/keys").get_data(as_text=True))
            page = web.app.test_client().get("/").get_data(as_text=True)
            self.assertIn("脱敏", page)

    def test_ai_redacts_keys_in_metadata_and_preflight_urls(self):
        hit = Hit(repo="example/demo", path="config.py", html_url=f"https://example.test/source?key={KEY}",
                  language="Python", full_text=f'API_KEY="{KEY}"\nbase_url="https://api.deepseek.com/v1?key={KEY}"')
        response = mock.Mock(status_code=200)
        response.json.return_value = {"choices": [{"message": {"content": json.dumps({"keys": [{
            "api_key": "KEY_CANDIDATE_1", "provider": "DeepSeek", "api_base": "https://api.deepseek.com/v1",
            "confidence": 5, "notes": "Synthetic fixture", "category": "llm"}]})}}]}
        with mock.patch("gks.analyzer.requests.post", return_value=response) as post:
            results = AIAnalyzer("https://example.test/v1", "synthetic-own-ai-key", "fixture", max_retries=0).analyze(hit)
        self.assertEqual(len(results), 1)
        outbound = json.dumps(post.call_args.kwargs["json"])
        self.assertNotIn(KEY, outbound)
        self.assertIn("KEY_CANDIDATE_1", outbound)


if __name__ == "__main__":
    unittest.main(verbosity=2)
