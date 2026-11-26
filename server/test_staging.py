"""Owner-scoped staging against disposable metadata/GridFS stores and a private HTTP fixture."""
import asyncio
from concurrent.futures import ProcessPoolExecutor
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
import multiprocessing
import os
from threading import BoundedSemaphore, Thread
import time
from unittest import TestCase, skipUnless
from unittest.mock import Mock, patch
from uuid import uuid4

from bson import ObjectId
from fastapi import HTTPException, Request
import gridfs

from . import api, intake, sources
from .db import db, media_db, now, setup_indexes, uses_postgres
from .postgres import install
from .source import TOO_LONG


def _start_node(barrier):
    global _start
    _start = barrier
    db().command("ping")


def _stage_on_node(job, endpoint):
    _start.wait(timeout=8)
    with patch.object(sources, "INTAKE", endpoint):
        return sources.for_job(job)


@skipUnless(os.getenv("REELDUB_TEST_DATABASE") == "1", "Requires disposable metadata and GridFS")
class StagingTests(TestCase):
    @classmethod
    def setUpClass(cls):
        if uses_postgres():
            with db().connection() as conn:
                install(conn)
        else:
            setup_indexes()

    def setUp(self):
        self.users = ["apple:" + str(uuid4()) for _ in range(2)]
        self.url = "https://www.instagram.com/reel/StageTrial/"
        self.payload = b"private source fixture"
        self.seconds = 17
        self.fetches = []
        self.enterContext(patch.dict(os.environ, {"REELDUB_OUTPUT_PROFILE": "studio", "REELDUB_CREATOR_MINUTES": "0"}))
        fixture = self

        class Intake(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                data = self.rfile.read(int(self.headers["Content-Length"]))
                if self.path == "/inspect":
                    fixture.fetches.append({"inspect": len(data)})
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"seconds": fixture.seconds}).encode())
                    return
                fixture.fetches.append(json.loads(data))
                time.sleep(0.12)  # Leave the cold preparation in progress for a competing process.
                self.send_response(200)
                self.send_header("Content-Length", str(len(fixture.payload)))
                self.send_header("X-Duration", str(fixture.seconds))
                self.send_header("X-Title", "Fixture%20title")
                self.send_header("X-Author", "Fixture%20author")
                self.end_headers()
                self.wfile.write(fixture.payload)

        http = ThreadingHTTPServer(("127.0.0.1", 0), Intake)
        self.addCleanup(http.server_close)
        thread = Thread(target=http.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        self.addCleanup(http.shutdown)
        self.endpoint = f"http://127.0.0.1:{http.server_port}"
        self.enterContext(patch.object(sources, "INTAKE", self.endpoint))
        self.bucket = gridfs.GridFS(media_db())
        self.addCleanup(self.clean)

    def clean(self):
        owners = {"owner": {"$in": self.users}}
        for rec in db().sources.find(owners):
            self.bucket.delete(rec["_id"])
        db().sources.delete_many(owners)
        db().jobs.delete_many(owners)

    def admitted(self, owner, target="en"):
        result = api.submit(api.Submission(url=self.url + "?share=fixture", request_id=uuid4(),
                                           target_language=target), owner)
        db().jobs.update_one({"_id": result["id"]}, {"$set": {
            "state": "running", "lease": str(uuid4()), "lease_end": now() + timedelta(minutes=3)}})
        return db().jobs.find_one({"_id": result["id"]})

    def test_two_servers_share_one_download_across_languages_retries_and_creator(self):
        owner = self.users[0]
        jobs = [self.admitted(owner, target) for target in ("en", "es")]
        ctx = multiprocessing.get_context("spawn")
        with ProcessPoolExecutor(2, mp_context=ctx, initializer=_start_node, initargs=(ctx.Barrier(2),)) as pool:
            tasks = [pool.submit(_stage_on_node, job, self.endpoint) for job in jobs]
            docs = [task.result(timeout=20) for task in tasks]
        self.assertEqual(docs[0]["_id"], docs[1]["_id"])
        source = docs[0]["_id"]
        self.assertEqual(self.bucket.get(source).read(), self.payload)
        self.assertEqual(self.fetches, [{"url": self.url, "daily": True}])
        db().sources.update_one({"_id": source}, {"$set": {"expires": now() + timedelta(minutes=2)}})
        self.assertEqual(sources.reel(owner, self.url + "?share=again")["id"], str(source))
        self.assertGreater(db().sources.find_one({"_id": source})["expires"], now() + timedelta(hours=23))
        self.assertEqual(sources.for_job(jobs[0])["_id"], source)
        self.assertEqual(len(self.fetches), 1)
        for original in jobs:
            attached = db().jobs.find_one({"_id": original["_id"]})
            self.assertNotIn("slot", attached)
            self.assertEqual((attached["staged_source"], attached["title"], attached["author"]),
                             (source, "Fixture title", "Fixture author"))
        self.assertNotEqual(sources.for_job(self.admitted(self.users[1]))["_id"], source)
        self.assertEqual(len(self.fetches), 2)

    def test_expired_preparation_is_recovered_and_stale_publisher_is_fenced(self):
        owner = self.users[0]
        stale, slot = ObjectId(), "reel:" + self.url
        db().sources.insert_one({"_id": stale, "owner": owner, "slot": slot,
                                 "expires": now() - timedelta(seconds=1)})
        doc = sources.for_job(self.admitted(owner))
        with self.assertRaises(HTTPException) as expired:
            sources.save(stale, owner, BytesIO(b"old source"), self.url, 1, "old", reel_slot=slot)
        self.assertEqual(expired.exception.status_code, 503)
        self.assertFalse(self.bucket.exists(stale))
        self.assertEqual(db().sources.count_documents({"owner": owner, "ready": True}), 1)
        self.assertEqual(self.bucket.get(doc["_id"]).read(), self.payload)

    def test_sweep_pins_active_input_then_reclaims_its_bytes_after_expiry(self):
        job = self.admitted(self.users[0])
        source = sources.for_job(job)["_id"]
        def expire():
            db().sources.update_one({"_id": source}, {"$set": {"expires": now() - timedelta(seconds=1)}})
            sources.sweep()
        expire()
        self.assertTrue(self.bucket.exists(source))
        db().jobs.update_one({"_id": job["_id"]}, {"$set": {"state": "done", "deleted": True}})
        self.assertTrue(self.bucket.exists(source))
        expire()
        self.assertFalse(self.bucket.exists(source))
        self.assertIsNone(db().sources.find_one({"_id": source}))

    def test_confirmed_upload_stays_owner_scoped_and_old_lease_cannot_fetch(self):
        owner = self.users[0]
        source = ObjectId()
        job = self.admitted(owner)
        with self.assertRaises(HTTPException) as stale:
            sources.for_job({**job, "lease": "obsolete"})
        self.assertEqual(stale.exception.status_code, 409)
        db().jobs.update_one({"_id": job["_id"]}, {"$set": {"url": "upload:fixture", "source_file": source,
                                                              "quoted_seconds": 6, "trimmed": True}})
        db().sources.insert_one({"_id": source, "owner": self.users[1], "identity": "upload:fixture",
                                 "seconds": 20, "title": "Private", "ready": True, "expires": now()})
        self.bucket.put(self.payload, _id=source)
        with self.assertRaises(HTTPException) as private:
            sources.for_job(job)
        self.assertEqual(private.exception.status_code, 410)
        db().sources.update_one({"_id": source}, {"$set": {"owner": owner}})
        self.assertEqual(sources.for_job(job)["_id"], source)
        self.assertEqual(db().jobs.find_one({"_id": job["_id"]})["quoted_seconds"], 6)
        self.assertFalse(self.fetches)

    def test_admission_accepts_multiple_jobs_without_eager_fetch(self):
        for _ in range(5):
            self.admitted(self.users[0])
        self.admitted(self.users[0])
        self.assertEqual(db().jobs.count_documents({"owner": self.users[0]}), 6)
        self.assertFalse(self.fetches)

    def test_creator_upload_inspects_once_and_keeps_private_bytes(self):
        owner = self.users[0]

        def upload():
            async def receive():
                return {"type": "http.request", "body": self.payload, "more_body": False}
            request = Request({"type": "http", "headers": [
                (b"content-type", b"video/quicktime"), (b"x-video-title", b"My%20import")
            ]}, receive)
            return asyncio.run(api.stage_upload(request, owner))

        result = upload()
        stage = ObjectId(result["id"])
        self.assertEqual(result, {"id": str(stage), "seconds": 17, "title": "My import"})
        self.assertEqual(upload(), result)
        self.assertEqual(self.fetches, [{"inspect": len(self.payload)}])
        self.assertEqual(self.bucket.get(stage).read(), self.payload)
        with self.assertRaises(HTTPException):
            sources.get(self.users[1], str(stage))

    @patch("server.sources.MAX_SOURCE_SECONDS", 120)
    def test_old_intake_cannot_stage_an_oversized_upload_or_reel(self):
        self.seconds = 121
        owner = self.users[0]
        with self.assertRaises(HTTPException) as upload:
            with sources.admission(owner) as stage:
                sources.upload(stage, owner, BytesIO(self.payload), "long", "Long clip")
        with self.assertRaises(HTTPException) as reel:
            sources.reel(owner, self.url)
        for result in (upload, reel):
            self.assertEqual((result.exception.status_code, result.exception.detail), (422, TOO_LONG))
        self.assertEqual(db().sources.count_documents({"owner": owner}), 0)
        self.assertEqual(db().jobs.count_documents({"owner": owner}), 0)
        self.assertFalse(self.bucket.exists(stage))

    def test_account_closing_during_staging_leaves_no_new_private_source(self):
        owner = self.users[0]
        job = self.admitted(owner)
        db().account_deletions.insert_one({"_id": owner, "expires": now() + timedelta(hours=24)})
        self.addCleanup(db().account_deletions.delete_one, {"_id": owner})
        with self.assertRaises(HTTPException) as closing:
            sources.for_job(job)
        self.assertEqual(closing.exception.status_code, 409)
        self.assertEqual(db().sources.count_documents({"owner": owner}), 0)
        self.assertFalse(self.fetches)


class IntakeTransportTests(TestCase):
    @patch("server.source.MAX_SOURCE_SECONDS", 120)
    def test_inspects_track_and_container_durations_before_rounding(self):
        path = Mock()
        path.stat.return_value.st_size = 1000
        for video, audio, container, expected in [
            ("119.999", "120.000", "120.000", 120),
            ("120.001", "119", "120.000", TOO_LONG),
            ("119", "120.001", "120", TOO_LONG),
            ("119", "120", "120.001", TOO_LONG),
            ("N/A", "N/A", "N/A", "Choose a playable MP4 or MOV with audio, up to 4K."),
        ]:
            media = {"format": {"duration": container}, "streams": [
                {"codec_type": "video", "codec_name": "h264", "width": 16, "height": 16, "duration": video},
                {"codec_type": "audio", "duration": audio}]}
            with self.subTest(video=video, audio=audio, container=container), patch(
                    "server.source.subprocess.check_output", return_value=json.dumps(media)):
                if expected == 120:
                    self.assertEqual(intake.probe(path), expected)
                else:
                    with self.assertRaises(HTTPException) as rejected:
                        intake.probe(path)
                    self.assertEqual((rejected.exception.status_code, rejected.exception.detail), (422, expected))

    def test_intake_always_releases_its_permit_on_disk_failure(self):
        slot = BoundedSemaphore(1)
        body = intake.Reel(url="https://www.instagram.com/reel/StageTrial/")
        with patch.object(intake, "slots", slot):
            with patch.object(intake, "TemporaryDirectory", side_effect=OSError("fixture full")), self.assertRaises(HTTPException) as failed:
                intake.fetch(body)
            self.assertEqual(failed.exception.status_code, 503)
            tmp = Mock(name="fixture")
            tmp.name = "/tmp"
            tmp.cleanup.side_effect = OSError("fixture cleanup")
            with patch.object(intake, "TemporaryDirectory", return_value=tmp), patch.object(intake, "download_reel", side_effect=ValueError), self.assertRaises(OSError):
                intake.fetch(body)
            self.assertTrue(slot.acquire(False))
            slot.release()

    def test_explicit_mtls_requires_complete_config_and_https(self):
        full = {"REELDUB_INTAKE_CA": "/ca", "REELDUB_INTAKE_CERT": "/cert", "REELDUB_INTAKE_KEY": "/key"}
        with patch.dict(os.environ, full), patch.object(sources, "INTAKE", "https://intake.test"):
            self.assertEqual(sources.intake_tls(), {"verify": "/ca", "cert": ("/cert", "/key")})
            with patch.dict(os.environ, {"REELDUB_INTAKE_KEY": ""}), self.assertRaises(HTTPException):
                sources.intake_tls()
        with patch.dict(os.environ, full), patch.object(sources, "INTAKE", "http://intake.test"), self.assertRaises(HTTPException):
            sources.intake_tls()
