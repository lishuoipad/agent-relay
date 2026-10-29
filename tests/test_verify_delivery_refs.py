"""Frozen nested-reference checks, without model or device calls."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from types import SimpleNamespace

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/parallel-dev-coordination/scripts"
sys.path.insert(0, str(SCRIPTS))
import verify_delivery_refs as verifier


class DeliveryRefsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.file = self.root / "candidate.py"
        self.file.write_bytes(b"known content\n")
        self.ref = {"path": str(self.file), "sha256": hashlib.sha256(self.file.read_bytes()).hexdigest()}
        self.manifest = self.root / "delivery.json"

    def check(self, source=None, **kwargs):
        self.manifest.write_text(json.dumps(source if source is not None else {"files": [self.ref]}), encoding="utf-8")
        sha = hashlib.sha256(self.manifest.read_bytes()).hexdigest()
        return verifier.verify(self.manifest, sha, **kwargs)

    def test_nested_repeated_refs_hash_once_without_following_documents(self):
        document = self.root / "other.json"
        document.write_text(json.dumps({"path": "unread/secret", "sha256": "a" * 64}), encoding="utf-8")
        other = {"path": str(document), "sha256": hashlib.sha256(document.read_bytes()).hexdigest()}
        result = self.check({"files": [self.ref, other], "review": {"candidate": self.ref}})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["refOccurrences"], 3)
        self.assertEqual(result["checkedFiles"], 2)

    def test_wrong_frozen_manifest_hash_reads_no_references(self):
        self.check()
        with mock.patch.object(verifier, "hash_file", side_effect=AssertionError("must not read")):
            result = verifier.verify(self.manifest, "0" * 64)
        self.assertFalse(result["ok"])
        self.assertEqual(result["checkedFiles"], 0)

    def test_mismatch_is_failure(self):
        self.file.write_text("changed", encoding="utf-8")
        result = self.check()
        self.assertFalse(result["ok"])
        self.assertEqual(result["files"][0]["error"], "content hash mismatch")

    def test_missing_file_is_failure(self):
        self.file.unlink()
        result = self.check()
        self.assertFalse(result["ok"])
        self.assertIn("error", result["files"][0])

    def test_conflicting_duplicate_is_failure(self):
        result = self.check([self.ref, {**self.ref, "sha256": "b" * 64}])
        self.assertFalse(result["ok"])
        self.assertEqual(result["checkedFiles"], 0)

    def test_relative_and_remote_paths_rejected(self):
        for path in ("relative.py", "//remote/share/file", "\\\\remote\\share\\file"):
            with self.subTest(path=path):
                self.assertFalse(self.check({"files": [{**self.ref, "path": path}]})["ok"])

    def test_malformed_and_empty_refs_cannot_pass(self):
        for source in ({"files": []}, {"sha256": "a" * 64}, {"path": str(self.file), "sha256": "invalid"}):
            with self.subTest(source=source):
                self.assertFalse(self.check(source)["ok"])

    def test_reference_and_read_budgets_fail_closed(self):
        self.assertFalse(self.check([self.ref, self.ref], max_refs=1)["ok"])
        self.assertFalse(self.check(max_bytes=1)["ok"])

    def test_manifest_changed_mid_verification_cannot_pass(self):
        original = verifier.hash_file
        def change(*args):
            result = original(*args)
            self.manifest.write_text("{}", encoding="utf-8")
            return result
        with mock.patch.object(verifier, "hash_file", side_effect=change):
            result = self.check()
        self.assertFalse(result["ok"])
        self.assertIn("manifest changed", result["errors"][0])

    def test_path_replaced_mid_read_cannot_pass(self):
        original = verifier.os.fstat
        calls = 0
        def replace(fd):
            nonlocal calls
            calls += 1
            if calls == 2:
                # Windows allows in-place content changes while this file is open.
                with self.file.open("ab") as stream:
                    stream.write(b"changed after read")
            return original(fd)
        with mock.patch.object(verifier.os, "fstat", side_effect=replace):
            result = self.check()
        self.assertFalse(result["ok"])
        self.assertIn("file changed", result["files"][0]["error"])

    def test_windows_stat_and_fstat_ctime_difference_is_not_a_false_change(self):
        original = verifier.os.fstat
        def different_ctime(fd):
            value = original(fd)
            fields = {key: getattr(value, key) for key in
                      ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")}
            fields["st_ctime_ns"] += 1000
            return SimpleNamespace(**fields)
        with mock.patch.object(verifier.os, "fstat", side_effect=different_ctime):
            self.assertTrue(self.check()["ok"])

    def test_cli_returns_compact_summary_and_preserves_existing_output(self):
        self.check()
        sha = hashlib.sha256(self.manifest.read_bytes()).hexdigest()
        output = self.root / "report.json"
        cmd = [sys.executable, "-B", str(SCRIPTS / "verify_delivery_refs.py"), str(self.manifest),
               "--expected-sha256", sha, "--report", str(output)]
        first = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(first.returncode, 0, first.stderr)
        compact = json.loads(first.stdout)
        self.assertTrue(compact["ok"])
        self.assertNotIn("files", compact)
        raw = output.read_bytes()
        self.assertEqual(compact["reportSha256"], hashlib.sha256(raw).hexdigest())
        again = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
        self.assertNotEqual(again.returncode, 0)
        self.assertEqual(output.read_bytes(), raw)


if __name__ == "__main__":
    unittest.main()
