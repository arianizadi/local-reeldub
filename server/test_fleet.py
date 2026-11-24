"""Private coordinator HTTP integration checks in disposable PostgreSQL + GridFS."""
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from hashlib import sha256
import json
import os
from pathlib import Path
import secrets
import socket
from tempfile import TemporaryDirectory
import threading
import time
from unittest import TestCase, skipUnless
from unittest.mock import patch
from uuid import uuid4

from bson import ObjectId
from fastapi import HTTPException
import gridfs
from psycopg import OperationalError
import requests
import uvicorn

from . import api, console, fleet, fleet_health, worker
from .db import db, media_db, now, separate_metadata, setup_indexes, uses_postgres
from .postgres import install
from .wakeup import notify
from .source import TOO_LONG


@contextmanager
def http_fixture(app, *, lifespan="auto"):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        address = "http://127.0.0.1:" + str(sock.getsockname()[1])
        server = uvicorn.Server(uvicorn.Config(app, lifespan=lifespan, access_log=False, log_level="error"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        try:
            for _ in range(200):
                if server.started:
                    break
                time.sleep(.01)
            else:
                raise RuntimeError("Fixture HTTP service did not start")
            yield address
        finally:
            server.should_exit = True
            thread.join(timeout=10)


@skipUnless(os.getenv("REELDUB_TEST_DATABASE") == "1" and separate_metadata(), "Requires disposable metadata and GridFS")
class FleetIntegration(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.enterClassContext(patch.dict(os.environ, {"REELDUB_FLEET_REGISTRY": '{"nodes":{}}'}))
        if uses_postgres():
            with db().connection() as conn:
                install(conn)
        else:
            setup_indexes()
        cls.base = cls.enterClassContext(http_fixture(fleet.app))

    def setUp(self):
        self.assertEqual(db().jobs.count_documents({"state": {"$in": ["queued", "running"]}}), 0,
                         "Use a dedicated idle test database")
        self.users = ["test:" + str(uuid4()) for _ in range(2)]
        self.tokens = {key: secrets.token_urlsafe(36) for key in ("alpha", "beta")}
        self.bucket = gridfs.GridFS(media_db())
        reg = {"nodes": {node: {"token_sha256": sha256(token.encode()).hexdigest(), "slots": 1,
                                "studio": node == "alpha"} for node, token in self.tokens.items()}}
        self.enterContext(patch.dict(os.environ, {"REELDUB_FLEET_REGISTRY": json.dumps(reg)}))
        self.addCleanup(media_db().fleet_nodes.delete_many, {"_id": {"$in": list(self.tokens)}})
        for node in self.tokens:
            self.assertEqual(self.req("POST", "/v1/slots/0/health", node,
                                      json={"ready": True, "preflight": True}).json()["status"], "online")
        fleet.slot_state.cache_clear()
        self.addCleanup(self.clean)

    def clean(self):
        fleet_health.tickets().delete_many({"node": {"$in": list(self.tokens)}})
        owners = {"owner": {"$in": self.users}}
        for job in db().jobs.find(owners):
            if job.get("artifact"):
                self.bucket.delete(job["artifact"])
        for src in db().sources.find(owners):
            self.bucket.delete(src["_id"])
        db().sources.delete_many(owners)
        db().jobs.delete_many(owners)

    def queue(self, seq=1, **fields):
        job = {"_id": str(uuid4()), "owner": self.users[seq % 2], "request_id": str(uuid4()),
               "state": "queued", "queue_seq": seq, "priority": 0, "created": now(),
               "url": "https://www.instagram.com/reel/FleetTrial/", "day": now().date().isoformat(),
               "attempt": 0, **fields}
        db().jobs.insert_one(job)
        notify()
        return job

    def req(self, method, path, node="alpha", lease=None, headers=None, **kwargs):
        auth = {"Authorization": "Bearer " + self.tokens[node], "X-ReelDub-Node": node}
        if lease:
            auth["X-ReelDub-Lease"] = lease
        return requests.request(method, self.base + path, headers={**auth, **(headers or {})}, timeout=12, **kwargs)

    def claim(self, node="alpha", request=None, slot=0):
        return self.req("POST", f"/v1/slots/{slot}/claim", node,
                        json={"request_id": request or str(uuid4()), "upload_voice_protocol": 1})

    def test_kitten_narration_requires_a_ready_studio_worker_and_matching_result(self):
        original = self.queue(url="upload:kitten", voice_mode="narrator", narrator_engine="kitten2")
        self.assertEqual(self.claim().status_code, 204)
        body = {"request_id": str(uuid4()), "kitten_narrator": True, "upload_voice_protocol": 1}
        self.assertEqual(self.req("POST", "/v1/slots/0/claim", node="beta", json=body).status_code, 204)
        job = self.req("POST", "/v1/slots/0/claim", json=body).json()
        self.assertEqual((job["_id"], job["narrator_engine"]), (original["_id"], "kitten2"))
        self.assertEqual(self.claim(request=body["request_id"]).status_code, 204)
        details = {"media_seconds": 5, "rendered_voice_mode": "narrator", "voice_model": "Kokoro v1.0"}
        headers = {"Content-Type": "application/octet-stream", "X-ReelDub-Details":
                   base64.urlsafe_b64encode(json.dumps(details).encode()).decode()}
        result = self.req("PUT", "/v1/jobs/" + job["_id"] + "/result", lease=job["lease"], headers=headers, data=b"")
        self.assertEqual(result.status_code, 422)
        self.assertIn("narrator", result.json()["detail"])

    def test_kitten_recovery_prefers_an_idle_compatible_gpu_then_reuses_its_own(self):
        registry = json.loads(os.environ["REELDUB_FLEET_REGISTRY"])
        registry["nodes"]["beta"]["studio"] = True
        self.enterContext(patch.dict(os.environ, {"REELDUB_FLEET_REGISTRY": json.dumps(registry)}))

        def claim_kitten(node):
            return self.req("POST", "/v1/slots/0/claim", node,
                            json={"request_id": str(uuid4()), "upload_voice_protocol": 1, "kitten_narrator": True})

        for node in ("alpha", "beta"):
            self.req("POST", "/v1/slots/0/health", node,
                     json={"ready": True, "preflight": True, "kitten_narrator": True}).raise_for_status()
        fields = {"url": "upload:kitten", "voice_mode": "narrator", "narrator_engine": "kitten2",
                  "recovery_rank": 1, "recovery_from_node": "alpha"}
        media_db().fleet_nodes.update_one({"_id": "alpha"}, {"$set": {"recoveries": 1}})
        first = self.queue(**fields)
        self.assertEqual(claim_kitten("alpha").status_code, 204)
        self.assertEqual(claim_kitten("beta").json()["_id"], first["_id"])

        # The second Studio worker returns online without Kitten installed.
        db().jobs.update_one({"_id": first["_id"]}, {"$set": {"state": "done"}})
        fleet_health.tickets().update_many({"node": "beta"}, {"$set": {"watch": False}})
        self.assertEqual(self.pulse("beta", check=True)["status"], "online")
        retry = self.queue(**fields)
        self.assertEqual(self.claim("beta").status_code, 204)
        self.assertEqual(claim_kitten("alpha").json()["_id"], retry["_id"])

    def test_two_nodes_claim_distinct_work_and_retries_cannot_reserve_extra_jobs(self):
        best = self.queue(1, priority=1, render_quality="best")
        regular = self.queue(2)
        untouched = self.queue(3)
        req = str(uuid4())
        with ThreadPoolExecutor(2) as pool:
            tasks = [pool.submit(self.claim, node, req) for node in ("alpha", "beta")]
            first, second = [task.result().json() for task in tasks]
        self.assertEqual([first["_id"], second["_id"]], [best["_id"], regular["_id"]])
        self.assertFalse({"owner", "url", "source_file", "slot", "request_id"} & first.keys())
        replay = self.claim("alpha", req).json()
        self.assertEqual((replay["_id"], replay["lease"]), (first["_id"], first["lease"]))
        self.assertGreater(replay["lease_remaining_seconds"], 0)
        self.assertEqual(self.claim("alpha").status_code, 204)
        self.assertEqual(self.claim(slot=1).status_code, 403)
        self.assertEqual(db().jobs.find_one({"_id": untouched["_id"]})["state"], "queued")

    def test_oversized_source_ends_job_before_render_and_releases_funding(self):
        raw = ObjectId()
        original = self.queue(slot=0, source_file=raw, quoted_seconds=60, trimmed=True)
        db().sources.insert_one({"_id": raw, "owner": original["owner"], "identity": original["url"],
                                 "ready": True, "seconds": 121, "title": "Long", "expires": now()+timedelta(hours=1)})
        claimed = self.claim().json()
        reply = self.req("GET", f"/v1/jobs/{claimed['_id']}/source", lease=claimed["lease"])
        self.assertEqual(reply.status_code, 409)
        failed = db().jobs.find_one({"_id": original["_id"]})
        self.assertEqual((failed["state"], failed["error"]), ("failed", TOO_LONG))
        self.assertFalse({"lease", "slot", "minute_units", "artifact"} & failed.keys())
        with patch.dict(os.environ, {"REELDUB_OUTPUT_PROFILE": "studio"}), self.assertRaises(HTTPException) as retry:
            api.submit(api.Submission(url=original["url"], target_language="es", request_id=uuid4()), original["owner"])
        self.assertEqual((retry.exception.status_code, retry.exception.detail), (422, TOO_LONG))
        self.assertEqual(db().jobs.count_documents({"owner": original["owner"]}), 1)
        self.assertEqual(self.claim().status_code, 204)

    def test_burst_worker_joins_and_recovers_existing_customers_job(self):
        owner = "apple:" + uuid4().hex
        self.users.append(owner)
        bearer = secrets.token_urlsafe(36)
        db().sessions.insert_one({"_id": api.hash_value(bearer), "owner": owner,
                                  "expires": now() + timedelta(minutes=5)})
        self.addCleanup(db().sessions.delete_one, {"_id": api.hash_value(bearer)})
        registry = {"nodes": {"alpha": fleet_health.registry()["alpha"]}}
        registry_file = Path(self.enterContext(TemporaryDirectory())) / "fleet-registry.json"
        registry_file.write_text(json.dumps(registry))
        self.enterContext(patch.dict(os.environ, {"REELDUB_FLEET_REGISTRY": str(registry_file),
                               "REELDUB_ACCESS_MODE": "hosted", "REELDUB_CREATOR_MINUTES": "0",
                               "REELDUB_OUTPUT_PROFILE": "studio"}))
        base = self.enterContext(http_fixture(api.app, lifespan="off"))
        client = self.enterContext(requests.Session())
        client.headers["Authorization"] = "Bearer " + bearer

        def submit():
            key = str(uuid4())
            url = f"https://www.instagram.com/reel/{uuid4().hex}/"
            # Stand in for a previously downloaded, private source. No external fetch.
            source_id = ObjectId()
            self.bucket.put(b"fixture source", _id=source_id)
            db().sources.insert_one({"_id": source_id, "owner": owner, "identity": url, "ready": True,
                                     "expires": now() + timedelta(hours=1), "seconds": 1, "title": "QA"})
            response = client.post(base + "/v1/jobs", json={"url": url, "request_id": key}, timeout=5)
            response.raise_for_status()
            return response.json()["id"]

        def progress(job_id):
            response = client.post(base + "/v1/library/progress", json={"ids": [job_id]}, timeout=5)
            response.raise_for_status()
            item, = response.json()
            self.assertEqual(item["id"], job_id)
            return item

        video = b"\x00\x00\x00\x18ftypisom" + b"synthetic fixture bytes"
        def complete(job):
            path = "/v1/jobs/" + job["_id"]
            source = self.req("GET", path + "/source", "rental", job["lease"])
            self.assertEqual((source.status_code, source.content), (200, b"fixture source"))
            details = base64.b64encode(b'{"media_seconds":1,"source_language":"en"}').decode()
            result = self.req("PUT", path + "/result", "rental", job["lease"], headers={
                "Content-Type": "application/octet-stream", "X-ReelDub-Details": details},
                data=bytes(4) + video)
            self.assertEqual(result.status_code, 204, result.text)

        first = submit()
        home = self.claim("alpha").json()
        self.assertEqual(home["_id"], first)
        second, third = submit(), submit()
        self.assertEqual(progress(second)["state"], "queued")
        self.tokens["rental"] = secrets.token_urlsafe(36)
        self.assertEqual(self.claim("rental").status_code, 401)
        registry["nodes"]["rental"] = {"slots": 1, "studio": True,
                                        "token_sha256": sha256(self.tokens["rental"].encode()).hexdigest()}
        next_registry = registry_file.with_suffix(".next")
        next_registry.write_text(json.dumps(registry))
        next_registry.replace(registry_file)
        self.addCleanup(media_db().fleet_nodes.delete_one, {"_id": "rental"})
        self.assertEqual(self.pulse("rental", check=True)["status"], "online")
        burst = self.claim("rental").json()
        self.assertEqual(burst["_id"], second)
        self.assertEqual([progress(key)["state"] for key in (first, second, third)], ["running", "running", "queued"])

        # Advance the home's heartbeat age; run the same watchdog as the coordinator.
        media_db().fleet_nodes.update_one({"_id": "alpha"}, {"$set": {"last_seen": now() - timedelta(seconds=85)}})
        self.scan()
        self.assertEqual(progress(first)["stage"], "Recovering render")
        complete(burst)
        takeover = self.claim("rental").json()
        self.assertEqual(takeover["_id"], first)
        self.assertTrue(progress(first)["recovery_switched_server"])
        self.assertEqual(progress(third)["state"], "queued")
        stale = self.req("POST", f"/v1/jobs/{first}/heartbeat", "alpha", home["lease"], json={})
        self.assertEqual(stale.status_code, 409)
        complete(takeover)
        self.assertEqual(progress(first)["state"], "done")
        delivered = client.get(f"{base}/v1/jobs/{first}/video", timeout=5)
        self.assertEqual((delivered.status_code, delivered.content), (200, video))
        allowance = client.get(base + "/v1/me", timeout=5)
        allowance.raise_for_status()
        self.assertEqual((allowance.json()["used"], allowance.json()["limit"]), (3, 5))

    def test_job_transfer_heartbeat_and_result_respect_owner_lease_trim_and_metadata(self):
        source = ObjectId()
        original = self.queue(source_file=source, url="upload:fixture", quoted_seconds=7)
        db().sources.insert_one({"_id": source, "owner": original["owner"], "identity": "upload:fixture",
                                 "ready": True, "seconds": 30, "title": "Original title", "author": "Fixture creator",
                                 "expires": now() + timedelta(hours=1)})
        self.bucket.put(b"sample input", _id=source)
        request_id = str(uuid4())
        # Even without an explicit voice mode, historical uploads must never go to a cloning-only worker.
        self.assertEqual(self.req("POST", "/v1/slots/0/claim", json={"request_id": str(uuid4())}).status_code, 204)
        job = self.claim(request=request_id).json()
        self.assertEqual(job["voice_mode"], "narrator")
        self.assertNotIn("voice_consent", job)
        self.assertEqual(self.req("POST", "/v1/slots/0/claim", json={"request_id": request_id}).status_code, 204)
        path = "/v1/jobs/" + job["_id"]
        lease = job["lease"]
        self.assertEqual(self.req("GET", path + "/source", "beta", lease).status_code, 409)
        stream = self.req("GET", path + "/source", lease=lease)
        self.assertEqual((stream.status_code, stream.content, stream.headers["X-Duration"]), (200, b"sample input", "7"))
        for progress in (.5, .3):
            self.assertEqual(self.req("POST", path + "/heartbeat", lease=lease,
                                      json={"stage": "Voicing", "progress": progress}).status_code, 204)
        self.assertEqual(db().jobs.find_one({"_id": job["_id"]})["progress"], .5)
        details = {"media_seconds": 7.02, "audio_mix_version": 2, "source_language": "ru"}
        head = {"Content-Type": "application/octet-stream", "X-ReelDub-Details":
                base64.urlsafe_b64encode(json.dumps(details).encode()).decode()}
        poster = b"\xff\xd8poster\xff\xd9"
        video = b"\x00\x00\x00\x18ftypisom" + b"sample" * 10
        def send(data):
            return self.req("PUT", path + "/result", lease=lease, headers=head, data=data)
        body = len(poster).to_bytes(4, "big") + poster + video
        self.assertEqual(send(body).status_code, 422)
        def report_voice(mode):
            head["X-ReelDub-Details"] = base64.urlsafe_b64encode(
                json.dumps({**details, "rendered_voice_mode": mode}).encode()).decode()
        report_voice("match")
        self.assertEqual(send(body).status_code, 422)
        details["voice_model"] = "KittenTTS 2 1.7B"
        report_voice("narrator")
        unexpected = send(body)
        self.assertEqual(unexpected.status_code, 422)
        self.assertIn("narrator", unexpected.json()["detail"])
        details["voice_model"] = "Kokoro v1.0"
        report_voice("narrator")
        self.assertEqual(send(bytes(4) + bytes(16)).status_code, 422)
        done = send(body)
        self.assertEqual(done.status_code, 204, done.text)
        final = db().jobs.find_one({"_id": job["_id"]})
        self.assertEqual((final["state"], final["title"], final["author"]), ("done", "Original title", "Fixture creator"))
        self.assertEqual((final["audio_mix_version"], final["source_language"]), (2, "ru"))
        self.assertEqual(final["rendered_voice_mode"], "narrator")
        stored = self.bucket.get(final["artifact"])
        self.assertEqual((stored.poster, stored.read()), (poster, video))
        self.assertEqual(self.claim(request=request_id).status_code, 204)
        self.assertEqual(self.req("POST", path + "/heartbeat", lease=lease, json={}).status_code, 409)

    def test_matching_upload_requires_scoped_consent_before_media_transfer(self):
        self.queue(url="upload:voice", voice_mode="match", minute_units=[str(uuid4())], slot=1)
        claimed = self.claim().json()
        result = self.req("GET", f"/v1/jobs/{claimed['_id']}/source", lease=claimed["lease"])
        self.assertEqual(result.status_code, 409)
        failed = db().jobs.find_one({"_id": claimed["_id"]})
        self.assertEqual(failed["state"], "failed")
        self.assertIn("requires consent", failed["error"])
        self.assertNotIn("minute_units", failed)
        self.assertNotIn("slot", failed)

    def test_preupgrade_upload_lease_moves_to_a_compatible_worker_before_source_transfer(self):
        self.queue(url="upload:legacy", slot=1)
        job = self.claim().json()
        db().jobs.update_one({"_id": job["_id"]}, {"$unset": {"fleet_upload_voice_protocol": ""}})
        result = self.req("GET", f"/v1/jobs/{job['_id']}/source", lease=job["lease"])
        self.assertEqual(result.status_code, 409)
        interrupted = db().jobs.find_one({"_id": job["_id"]})
        self.assertEqual((interrupted["state"], interrupted["recovery_rank"], interrupted["slot"]), ("queued", 1, 1))
        self.assertEqual(self.claim("beta").json()["voice_mode"], "narrator")

    def test_fenced_failure_releases_only_assigned_job_and_expired_worker_cannot_extend(self):
        self.queue(slot=2)
        job = self.claim().json()
        path = "/v1/jobs/" + job["_id"]
        failure = {"stage": "Translating", "reason": "translation"}
        self.assertEqual(self.req("POST", path + "/failure", "beta", job["lease"], json=failure).status_code, 409)
        self.assertEqual(self.req("POST", path + "/failure", lease=job["lease"], json=failure).status_code, 204)
        row = db().jobs.find_one({"_id": job["_id"]})
        self.assertEqual(row["state"], "failed")
        self.assertNotIn("slot", row)
        self.queue(2)
        second = self.claim().json()
        db().jobs.update_one({"_id": second["_id"]}, {"$set": {"lease_end": now() - timedelta(seconds=1)}})
        base = "/v1/jobs/" + second["_id"]
        self.assertEqual(self.req("POST", base + "/heartbeat", lease=second["lease"], json={}).status_code, 409)
        self.assertEqual(self.req("POST", base + "/failure", lease=second["lease"], json=failure).status_code, 409)
        self.assertEqual(db().jobs.find_one({"_id": second["_id"]})["state"], "running")

    def test_idle_gate_and_auth(self):
        notify()
        invalid = self.req("POST", "/v1/slots/0/claim", headers={"X-ReelDub-Node": "beta"},
                           json={"request_id": str(uuid4())})
        self.assertEqual(invalid.status_code, 401)
        oversized = self.req("POST", "/v1/slots/0/claim", data=b" " * 4097)
        self.assertEqual(oversized.status_code, 413)
        with patch.object(worker, "claim", wraps=worker.claim) as dequeue:
            for _ in range(6):
                self.assertEqual(self.claim().status_code, 204)
            self.assertEqual(dequeue.call_count, 1)
            self.queue()
            self.assertEqual(self.claim().status_code, 200)
            self.assertEqual(dequeue.call_count, 2)

    def test_telemetry_is_node_scoped_rate_limited_and_expires_from_admin(self):
        target = "/v1/slots/0/telemetry"
        sample = {"name": "Fixture GPU", "load": 65, "used_mib": 4096, "total_mib": 16384,
                  "temperature": 62, "watts": 180.5}
        store = media_db().fleet_telemetry
        self.addCleanup(store.delete_many, {"_id": {"$in": ["alpha", "beta"]}})
        with patch.object(fleet, "db", side_effect=AssertionError("idle telemetry touched PostgreSQL")):
            self.assertEqual(self.req("POST", target, json=sample).status_code, 204)
            self.assertEqual(self.req("POST", target, json={**sample, "load": 90}).status_code, 204)
            self.assertEqual(self.req("POST", target, "beta", json={**sample, "name": "Second GPU"}).status_code, 204)
            self.assertEqual(self.req("POST", "/v1/slots/1/telemetry", json=sample).status_code, 403)
            self.assertEqual(self.req("POST", target, json={**sample, "used_mib": 17000}).status_code, 422)
        values = console.metrics()
        self.assertEqual(values["gpu"], values["fleet"][0])
        self.assertEqual({row["node"]: row["load"] for row in values["fleet"]}, {"alpha": 65, "beta": 65})
        store.update_many({"_id": {"$in": ["alpha", "beta"]}}, {"$set": {"sampled": now() - timedelta(minutes=4)}})
        with patch.object(console, "gpu_snapshot", return_value=None):
            values = console.metrics()
        self.assertEqual(values["fleet"], [])
        self.assertIsNone(values["gpu"])

    def test_unknown_claim_outcome_never_assigns_a_second_job_to_same_request(self):
        original = self.queue(slot=1)
        req = str(uuid4())
        with patch.object(worker, "claim", side_effect=OperationalError("fixture interrupted")):
            self.assertEqual(self.claim(request=req).status_code, 503)
        self.assertEqual(self.claim(request=req).status_code, 204)
        retry = self.claim().json()
        self.assertEqual(retry["_id"], original["_id"])
        self.assertEqual(db().jobs.find_one({"_id": original["_id"]})["slot"], 1)

    def scan(self):
        media_db().maintenance.update_one({"_id": "fleet-watchdog"}, {"$set": {"due": now() - timedelta(seconds=1)}})
        fleet_health.watchdog()

    def pulse(self, node="alpha", *, check=False):
        return self.req("POST", "/v1/slots/0/health", node, json={"ready": True, "preflight": check}).json()

    def infra(self, job, node="alpha"):
        return self.req("POST", "/v1/jobs/" + job["_id"] + "/infrastructure", node, job["lease"],
                        json={"reason": "gpu_unavailable"})

    def test_offline_node_requeues_only_its_render_at_the_front(self):
        original = self.queue(slot=1)
        assigned = self.claim().json()
        newer = self.queue(2, priority=1)
        media_db().fleet_nodes.update_one({"_id": "alpha"}, {"$set": {"last_seen": now() - timedelta(seconds=85)}})
        self.scan()
        old = db().jobs.find_one({"_id": original["_id"]})
        self.assertEqual((old["state"], old["stage"], old["recovery_from_node"], old["slot"]),
                         ("queued", "Recovering render", "alpha", 1))
        self.assertEqual((old["recovery_count"], old["recovery_rank"]), (1, 1))
        self.assertEqual(self.req("POST", "/v1/jobs/" + assigned["_id"] + "/heartbeat",
                                  lease=assigned["lease"], json={}).status_code, 409)
        self.assertEqual(self.claim("beta").json()["_id"], assigned["_id"])
        self.assertEqual(db().jobs.find_one({"_id": newer["_id"]})["state"], "queued")
        self.assertEqual(self.pulse(), {"needs_preflight": True, "status": "offline"})
        self.assertEqual(self.claim().status_code, 204)
        self.assertEqual(self.pulse(check=True)["status"], "online")

    def test_single_node_needs_cooldown_and_verified_preflight_and_keeps_funding(self):
        initial = self.queue(slot=3, minute_units=["fixture#1"], minute_grants=["fixture"])
        job = self.claim().json()
        for attempt in range(1, worker.MAX_ATTEMPTS + 1):
            self.assertEqual(self.infra(job).status_code, 204)
            row = db().jobs.find_one({"_id": initial["_id"]})
            if attempt == worker.MAX_ATTEMPTS:
                self.assertEqual(row["state"], "failed")
                self.assertNotIn("slot", row)
                self.assertNotIn("minute_units", row)
                break
            self.assertEqual((row["state"], row["slot"], row["minute_units"]), ("queued", 3, ["fixture#1"]))
            self.assertEqual(self.pulse(check=True)["status"], "quarantined")
            self.assertEqual(self.claim().status_code, 204)
            report = media_db().fleet_nodes.find_one({"_id": "alpha"})
            self.assertGreater((report["blocked_until"] - now()).total_seconds(), 115 if attempt == 2 else 55)
            media_db().fleet_nodes.update_one({"_id": "alpha"}, {"$set": {"blocked_until": now() - timedelta(seconds=1)}})
            self.assertTrue(self.pulse()["needs_preflight"])
            self.assertEqual(self.pulse(check=True)["status"], "online")
            job = self.claim().json()
            self.assertEqual(job["_id"], initial["_id"])

    def test_admission_probe_failure_must_be_confirmed_before_interrupting_a_job(self):
        self.queue()
        job = self.claim().json()
        nodes = media_db().fleet_nodes
        key = {"_id": "alpha"}

        def failed_probe(checked=True):
            return self.req("POST", "/v1/slots/0/health",
                            json={"ready": False, "preflight": checked, "reason": "runtime_unavailable"}).json()

        beat = "/v1/jobs/" + job["_id"] + "/heartbeat"
        self.assertEqual(failed_probe()["status"], "suspect")
        self.assertTrue(failed_probe()["needs_preflight"])
        self.assertEqual(self.claim().status_code, 204)
        self.scan()
        self.assertEqual(self.req("POST", beat, lease=job["lease"], json={}).status_code, 204)
        self.assertEqual(db().jobs.find_one({"_id": job["_id"]})["attempt"], 1)
        self.assertEqual(self.pulse(check=True)["status"], "online")
        self.assertNotIn("probe_failed_at", nodes.find_one(key))
        self.assertEqual(failed_probe()["status"], "suspect")
        nodes.update_one(key, {"$set": {"probe_failed_at": now() - timedelta(seconds=31)}})
        self.assertEqual(failed_probe(checked=False)["status"], "suspect")
        self.assertEqual(failed_probe()["status"], "quarantined")
        self.assertEqual(self.req("POST", beat, lease=job["lease"], json={}).status_code, 409)
        self.scan()
        self.assertEqual(db().jobs.find_one({"_id": job["_id"]})["recovery_count"], 1)

    def test_only_a_current_lease_can_refresh_health_and_missing_health_is_retryable(self):
        self.queue()
        job = self.claim().json()
        path = "/v1/jobs/" + job["_id"] + "/heartbeat"
        key = {"_id": "alpha"}
        nodes = media_db().fleet_nodes
        nodes.update_one(key, {"$set": {"last_seen": now() - timedelta(seconds=12)}})
        older = nodes.find_one(key)["last_seen"]
        self.assertEqual(self.req("POST", path, lease=str(uuid4()), json={}).status_code, 409)
        self.assertEqual(nodes.find_one(key)["last_seen"], older)
        nodes.delete_one(key)
        self.assertEqual(self.req("POST", path, lease=job["lease"], json={}).status_code, 503)
        self.assertEqual(self.req("POST", path, lease=str(uuid4()), json={}).status_code, 409)
        self.assertTrue(self.pulse()["needs_preflight"])
        self.assertEqual(self.req("POST", path, lease=job["lease"], json={}).status_code, 503)
        self.assertEqual(self.pulse(check=True)["status"], "online")
        self.assertEqual(self.req("POST", path, lease=job["lease"], json={}).status_code, 204)
        nodes.update_one(key, {"$set": {"last_seen": now() - timedelta(seconds=85)}})
        self.assertTrue(self.pulse()["needs_preflight"])
        self.assertEqual(self.req("POST", path, lease=job["lease"], json={}).status_code, 503)
        self.assertEqual(self.pulse(check=True)["status"], "online")
        self.assertEqual(self.req("POST", path, lease=job["lease"], json={}).status_code, 204)

    def test_heartbeat_cannot_extend_attempt_beyond_hard_deadline(self):
        self.queue()
        job = self.claim().json()
        db().jobs.update_one({"_id": job["_id"]}, {"$set": {
            "attempt_started": now() - fleet_health.MAX_ATTEMPT_AGE + timedelta(seconds=15)}})
        self.assertEqual(self.req("POST", "/v1/jobs/" + job["_id"] + "/heartbeat",
                                  lease=job["lease"], json={}).status_code, 409)

    def test_idle_watchdog_uses_no_postgres_and_transient_scan_retries_immediately(self):
        self.scan()  # Installation bootstrap may reconcile pre-existing active assignments once.
        with patch.object(fleet_health, "db", side_effect=AssertionError("Idle watchdog touched PostgreSQL")):
            self.scan()
        self.assertEqual(self.claim().status_code, 204)
        self.queue()
        with patch.object(fleet.claim_capacity, "acquire", return_value=False):
            self.assertEqual(self.claim().status_code, 503)
        self.assertEqual(self.claim().status_code, 200)

    def test_partial_gridfs_write_is_reclaimed_after_coordinator_restart(self):
        blob = ObjectId()
        media = media_db()
        media["fs.chunks"].insert_one({"files_id": blob, "n": 0, "data": b"incomplete"})
        media.fleet_uploads.insert_one({"_id": blob, "job": str(uuid4()), "token": str(uuid4()),
                                       "due": now() - timedelta(seconds=1)})
        self.scan()
        self.assertIsNone(media["fs.chunks"].find_one({"files_id": blob}))
        self.assertIsNone(media.fleet_uploads.find_one({"_id": blob}))

    def test_recovered_node_defers_only_to_an_idle_compatible_node(self):
        config = json.loads(os.environ["REELDUB_FLEET_REGISTRY"])
        config["nodes"]["beta"]["studio"] = True
        self.enterContext(patch.dict(os.environ, {"REELDUB_FLEET_REGISTRY": json.dumps(config)}))
        self.pulse("beta", check=True)
        self.queue(render_quality="best")
        failed = self.claim().json()
        self.assertEqual(self.infra(failed).status_code, 204)
        media_db().fleet_nodes.update_one({"_id": "alpha"}, {"$set": {"blocked_until": now() - timedelta(seconds=1)}})
        self.assertEqual(self.pulse(check=True)["status"], "online")
        self.assertEqual(self.claim().status_code, 204)
        self.assertEqual(self.claim("beta").json()["_id"], failed["_id"])

        # Beta is now rendering and cannot receive the next retry. Alpha may recover alone.
        self.queue(2, render_quality="best")
        second = self.claim().json()
        self.assertEqual(self.infra(second).status_code, 204)
        media_db().fleet_nodes.update_one({"_id": "alpha"}, {"$set": {"blocked_until": now() - timedelta(seconds=1)}})
        self.pulse(check=True)
        self.assertEqual(self.claim().json()["_id"], second["_id"])

    def test_quarantine_repairs_other_slot_even_if_its_ticket_was_late_and_node_is_draining(self):
        queued = self.queue()
        job = self.claim().json()
        fleet_health.fault("alpha", "gpu_unavailable")
        media_db().fleet_nodes.update_one({"_id": "alpha"}, {"$set": {"enabled": False}})
        fleet_health.tickets().update_one({"job": job["_id"]}, {"$set": {"due": now() + timedelta(minutes=2)}})
        self.scan()
        row = db().jobs.find_one({"_id": queued["_id"]})
        self.assertEqual((row["state"], row["recovery_reason"]), ("queued", "gpu_unavailable"))
