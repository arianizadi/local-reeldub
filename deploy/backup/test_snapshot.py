"""Exercise real replica-set snapshot isolation and round-trip BSON against a disposable mongod."""
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import unittest

from bson import ObjectId
from pymongo import MongoClient

from snapshot import capture, restore_isolated


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
                    original = capture(source, archive)
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
