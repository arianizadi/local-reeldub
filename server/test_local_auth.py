"""Exercise tenant isolation over HTTP against a separately provisioned disposable instance."""
import os
import secrets
import socket
import threading
import time
from uuid import uuid4
from unittest import TestCase, skipUnless
from unittest.mock import patch

import gridfs
import psycopg
import requests
import uvicorn

from .api import app
from .access_keys import manage
from .db import db, media_db


@skipUnless(os.getenv("REELDUB_TEST_DATABASE") == "1" and os.getenv("REELDUB_TEST_KEY_ADMIN_DSN")
            and os.getenv("REELDUB_AUTH_MODE") == "postgres", "Requires disposable local PostgreSQL auth")
class LocalKeyHTTP(TestCase):
    def setUp(self):
        self.admin = self.enterContext(psycopg.connect(os.environ["REELDUB_TEST_KEY_ADMIN_DSN"], autocommit=True))
        self.shared_key = secrets.token_urlsafe(32)
        self.enterContext(patch.dict(os.environ, {"SELF_HOST_KEY": self.shared_key}))
        sock = self.enterContext(socket.socket())
        sock.bind(("127.0.0.1", 0))
        self.endpoint = f"http://127.0.0.1:{sock.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app, lifespan="off", access_log=False, log_level="error"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        def stop():
            server.should_exit = True
            thread.join(timeout=10)
        self.addCleanup(stop)
        for _ in range(100):
            if server.started:
                break
            time.sleep(.02)
        self.assertTrue(server.started)
        self.client = self.enterContext(requests.Session())
        self.client.trust_env = False

    def call(self, method, path, key=None, **options):
        return self.client.request(method, self.endpoint + path, timeout=10,
                                   headers={"Authorization": f"Bearer {key}"} if key else {}, **options)

    def library(self):
        label = uuid4().hex
        key = manage(self.admin, "create", label)
        library, = self.admin.execute("SELECT library_id FROM reeldub.access_keys WHERE label=%s", (label,)).fetchone()
        self.addCleanup(self.admin.execute, "DELETE FROM reeldub.access_keys WHERE label=%s", (label,))
        self.addCleanup(db().jobs.delete_many, {"owner": "local:" + str(library)})
        self.addCleanup(db().playback.delete_many, {"owner": "local:" + str(library)})
        return label, key

    def test_keys_scope_jobs_and_rotation_invalidates_existing_playback(self):
        label, alice = self.library()
        _, bob = self.library()
        endpoint = "/v1/me"
        owner_id = self.call("GET", endpoint, alice).json()["library_id"]
        self.assertNotEqual(owner_id, self.call("GET", endpoint, bob).json()["library_id"])
        self.assertEqual(self.call("GET", endpoint, self.shared_key).status_code, 401)
        self.assertEqual(self.call("GET", endpoint).status_code, 401)
        url = "https://example.org/watch?signature=" + secrets.token_urlsafe(4400)
        response = self.call("POST", "/v1/jobs", alice, json={"url": url})
        self.assertEqual(response.status_code, 200)
        job = response.json()["id"]
        path = "/v1/jobs/" + job
        self.assertEqual(self.call("GET", "/v1/jobs", bob).json(), [])
        for method, suffix, params in [("GET", "", {}), ("POST", "/queue", {"json": {"position": "first"}}),
                                       ("POST", "/cancel", {}), ("POST", "/playback", {}), ("DELETE", "", {})]:
            self.assertEqual(self.call(method, path + suffix, bob, **params).status_code, 404)
        bucket = gridfs.GridFS(media_db())
        file_id = bucket.put(b"private test media")
        self.addCleanup(bucket.delete, file_id)
        db().jobs.update_one({"_id": job}, {"$set": {"state": "done", "artifact": file_id}})
        def ticket(key):
            token = self.call("POST", path + "/playback", key).json()["ticket"]
            return "/v1/play/" + job + "/" + token
        original_play = ticket(alice)
        self.assertEqual(self.call("GET", original_play).content, b"private test media")
        replacement = manage(self.admin, "rotate", label)
        self.assertEqual(self.call("GET", endpoint, alice).status_code, 401)
        self.assertEqual(self.call("GET", endpoint, replacement).json()["library_id"], owner_id)
        self.assertEqual(self.call("GET", original_play).status_code, 404)
        self.assertEqual(self.call("GET", path, replacement).json()["id"], job)
        next_play = ticket(replacement)
        manage(self.admin, "revoke", label)
        self.assertEqual(self.call("GET", endpoint, replacement).status_code, 401)
        self.assertEqual(self.call("HEAD", next_play).status_code, 404)
        self.assertEqual(self.call("GET", endpoint, bob).status_code, 200)
