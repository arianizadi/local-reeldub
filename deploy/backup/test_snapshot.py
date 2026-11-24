"""Exercise real replica-set snapshot isolation and round-trip BSON against a disposable mongod."""
import os
from pathlib import Path
from hashlib import sha256
import json
import socket
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from bson import BSON, ObjectId
from pymongo import MongoClient

from snapshot import COLLECTIONS, capture, restore_isolated, verify_snapshot


class ArchiveIntegrityTest(unittest.TestCase):
    def write_archive(self, path, accounts, expected):
        manifest = {"format": 1, "started_at": "2026-01-01T00:00:00+00:00", "collections": {}}
        with ZipFile(path, "w") as file:
            for name in COLLECTIONS:
                file.writestr(name + ".bson", accounts if name == "accounts" else b"")
                data = expected if name == "accounts" else b""
                manifest["collections"][name] = {"count": int(bool(data)), "sha256": sha256(data).hexdigest()}
            file.writestr("manifest.json", json.dumps(manifest))
        return manifest

    def test_changed_documents_and_counts_in_readable_zip(self):
        original = BSON.encode({"_id": 1, "name": "Original"})
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.zip"
            manifest = self.write_archive(path, original, original)
            verify_snapshot(path, manifest)
            for data in (BSON.encode({"_id": 1, "name": "Changed"}), b"", original + original):
                with self.subTest(data_length=len(data)):
                    self.write_archive(path, data, original)
                    with self.assertRaisesRegex(ValueError, "validation failed for accounts"):
                        verify_snapshot(path, manifest)
            replaced = self.write_archive(path, b"", b"")
            with self.assertRaisesRegex(ValueError, "manifest differs"):
                verify_snapshot(path, manifest)
            verify_snapshot(path, replaced)

    def test_invalid_bson_with_matching_digest(self):
        row = BSON.encode({"_id": 1, "nested": {"field": True}})
        corrupted = row.replace(b"\x08field", b"\x7ffield")
        self.assertNotEqual(row, corrupted)
        for data in (corrupted, row[:-2], b"\xff\xff\xff\x7f"):
            with self.subTest(data_length=len(data)), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "test.zip"
                manifest = self.write_archive(path, data, data)
                with self.assertRaisesRegex(ValueError, "Invalid BSON"):
                    verify_snapshot(path, manifest)

    def test_failed_verification_preserves_last_good_upload(self):
        import archive

        with tempfile.TemporaryDirectory() as folder:
            status = Path(folder) / "status.json"
            status.write_text('{"key":"previous-good-snapshot"}')
            with patch.dict(os.environ, {"STATE_DIRECTORY": folder, "REELDUB_BACKUP_DATABASE": "test"}), \
                    patch.object(archive, "mongo"), patch.object(archive, "s3") as cloud, \
                    patch("snapshot.capture", side_effect=lambda db, path, **kw: self.write_archive(path, b"bad", b"bad")):
                with self.assertRaisesRegex(ValueError, "Invalid BSON"):
                    archive.save(Path(folder) / "unused-uri")
            cloud.assert_not_called()
            self.assertEqual(json.loads(status.read_text()), {"key": "previous-good-snapshot"})

    def test_source_scan_runs_once_per_utc_day_and_retries_on_failure(self):
        import archive

        def capture_fixture(db, path, *, started_at, verify_source_counts):
            calls.append(verify_source_counts)
            if len(calls) == 1:
                raise ValueError("test collection scan mismatch")
            return self.write_archive(path, b"", b"")

        calls = []
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
                "STATE_DIRECTORY": folder, "REELDUB_BACKUP_DATABASE": "test"}), \
                patch.object(archive, "mongo"), patch.object(archive, "s3"), \
                patch.object(archive, "put_once") as put, patch("snapshot.capture", side_effect=capture_fixture):
            uri = Path(folder) / "unused-uri"
            status = Path(folder) / "status.json"
            with self.assertRaisesRegex(ValueError, "scan mismatch"):
                archive.save(uri)
            put.assert_not_called()
            self.assertFalse(status.exists())
            archive.save(uri)
            archive.save(uri)
            previous = json.loads(status.read_text())
            previous["source_counts_checked_on"] = "2000-01-01"
            status.write_text(json.dumps(previous))
            archive.save(uri)
            self.assertEqual(calls, [True, True, False, True])
            self.assertEqual(put.call_count, 3)


class SnapshotTest(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as folder, socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
            probe.close()
            process = subprocess.Popen([os.environ.get("MONGOD", "mongod"), "--dbpath", folder,
                "--replSet", "backupTest", "--bind_ip", "127.0.0.1", "--port", str(port),
                "--logpath", folder + "/mongod.log", "--wiredTigerCacheSizeGB", "0.25"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                with MongoClient(f"mongodb://127.0.0.1:{port}/?directConnection=true",
                                 serverSelectionTimeoutMS=7000, tz_aware=True) as client:
                    client.admin.command("replSetInitiate", {"_id": "backupTest",
                        "members": [{"_id": 0, "host": f"127.0.0.1:{port}"}]})
                    for _ in range(100):
                        if client.admin.command("hello").get("isWritablePrimary"):
                            break
                        time.sleep(0.1)
                with MongoClient(f"mongodb://127.0.0.1:{port}/?replicaSet=backupTest",
                                 serverSelectionTimeoutMS=7000, tz_aware=True) as client:
                    source = client.reeldub_fixture
                    source.accounts.insert_one({"_id": "apple:example", "tier": "beta"})
                    artifact = ObjectId()
                    source.jobs.insert_one({"_id": artifact, "owner": "apple:example", "poster": b"private"})
                    source.sessions.insert_one({"_id": "expired-session"})
                    archive = Path(folder) / "snapshot.zip"
                    original = capture(source, archive, verify_source_counts=True)
                    verify_snapshot(archive, original)
                    with patch("pymongo.collection.Collection.count_documents", return_value=42):
                        with self.assertRaisesRegex(ValueError, "collection scan and exported count differ"):
                            capture(source, Path(folder) / "mismatch.zip", verify_source_counts=True)
                    source.accounts.delete_one({"_id": "apple:example"})
                    target = client.reeldub_restore_fixture
                    self.assertEqual(original, restore_isolated(target, archive))
                    self.assertEqual(target.accounts.find_one()["tier"], "beta")
                    self.assertEqual(target.jobs.find_one(), {"_id": artifact, "owner": "apple:example"})
                    self.assertEqual(target.sessions.count_documents({}), 0)
                    with self.assertRaises(ValueError):
                        restore_isolated(target, archive)
                    with self.assertRaises(ValueError):
                        restore_isolated(source, archive)
                    source.unreviewed.insert_one({"secret": 7})
                    with self.assertRaisesRegex(ValueError, "unrecognized"):
                        capture(source, Path(folder) / "unknown.zip")
            finally:
                process.terminate()
                process.wait(timeout=15)


if __name__ == "__main__":
    unittest.main()
