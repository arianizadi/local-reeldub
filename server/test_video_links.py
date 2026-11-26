"""Public URL policy, pinned DNS and a native HLS download through a disposable proxy."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.util import find_spec
from pathlib import Path
import json
import shutil
import socket
import subprocess
import threading
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from .download import download_reel
from .media_network import connect_public
from .source import inspect_video
from .video_url import video_url


class URLPolicy(unittest.TestCase):
    def test_public_sites_keep_video_identifiers_and_signatures(self):
        for url in ("https://www.youtube.com/watch?v=example_1&list=example",
                    "http://videos.example.net/path.mp4?token=A%2FB&expires=88",
                    "https://vm.tiktok.com/voorbeeld/", "https://vimeo.com/1234567/secret",
                    "https://video.example.net/embed/#/video/104"):
            self.assertEqual(video_url(url), url)
        self.assertEqual(video_url("https://m.instagram.com/reels/Abcd_12/?igsh=tracker"),
                         "https://www.instagram.com/reel/Abcd_12/")
        self.assertEqual(video_url("https://MEDIA.Example.Org:443/clip?q=1"), "https://media.example.org/clip?q=1")

    def test_submit_requires_public_http_and_no_credentials(self):
        for url in ("/movie.mp4", "file:///movie.mp4", "ytsearch:film", "https://localhost/video",
                    "https://127.0.0.1/test", "https://[::1]/x", "http://169.254.0.1/x", "http://[ff02::01]/",
                    "https://[64:ff9b::7f00:1]/test", "http://[::ffff:127.0.0.1]/",
                    "https://example.org:8181/video", "https://demo:password@example.org/clip",
                    "https://example.org\n/redir", "https://example.org\\/file", "http://0x7f000001/",
                    "https://example.org/a#__youtubedl_smuggle", "http://0x7f.1/"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                video_url(url)

    def test_dns_changes_and_mixed_answers_are_rechecked_before_every_connection(self):
        answer = lambda address: (socket.AF_INET, socket.SOCK_STREAM, 0, "", (address, 443))
        with patch("socket.getaddrinfo", side_effect=[[answer("1.1.1.1")],
                   [answer("1.1.1.1"), answer("127.0.0.1")]]) as resolve, patch("socket.socket") as sock:
            self.assertIs(connect_public("media.example.org", 443), sock.return_value)
            sock.return_value.connect.assert_called_once_with(("1.1.1.1", 443))
            with self.assertRaises(ValueError):
                connect_public("media.example.org", 443)
            self.assertEqual(resolve.call_count, 2)
            self.assertEqual(sock.call_count, 1)

    def test_http_and_https_ports_are_validated(self):
        with patch("socket.getaddrinfo") as resolve, self.assertRaises(ValueError):
            connect_public("media.example.org", 8080)
        resolve.assert_not_called()


@contextmanager
def clips(folder, get_media):
    class Fixture(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            get_media(self, folder)

    with ThreadingHTTPServer(("127.0.0.1", 0), Fixture) as http:
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        connections = []
        def origin(host, port):
            connections.append((host, port))
            if host not in ("media.example.org", "cdn.example.net") or port != 80:
                raise ValueError("Not a fixture host")
            return socket.create_connection(http.server_address)
        try:
            with patch("server.media_network.connect_public", side_effect=origin):
                yield connections
        finally:
            http.shutdown()
            thread.join()


@unittest.skipUnless(find_spec("yt_dlp") and shutil.which("ffmpeg") and shutil.which("ffprobe"),
                     "Requires the intake dependencies")
class IntakeDownload(unittest.TestCase):
    def test_redirect_manifest_and_each_segment_are_fetched_through_the_proxy(self):
        def media(handler, folder):
            path = handler.path.split("?", 1)[0]
            if path == "/shared":
                handler.send_response(302)
                handler.send_header("Location", "http://cdn.example.net/video.m3u8")
                handler.end_headers()
                return
            item = folder / path.removeprefix("/")
            self.assertIn(item.name, ("video.m3u8", "video0.ts", "video1.ts"))
            data = item.read_bytes()
            handler.send_response(200)
            handler.send_header("Content-Type", "application/vnd.apple.mpegurl" if item.suffix == ".m3u8" else "video/mp2t")
            handler.send_header("Content-Length", str(len(data)))
            handler.end_headers()
            handler.wfile.write(data)

        with TemporaryDirectory() as tmp, TemporaryDirectory() as out:
            folder = Path(tmp)
            subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", "color=s=32x32:r=4",
                            "-f", "lavfi", "-i", "sine=frequency=250:sample_rate=16000", "-t", "2", "-threads", "1",
                            "-c:v", "libx264", "-g", "4", "-c:a", "aac", "-f", "hls", "-hls_time", "1",
                            "-hls_list_size", "0", folder / "video.m3u8"], check=True, timeout=15)
            with clips(folder, media) as connections:
                result = download_reel("http://media.example.org/shared", Path(out), metadata=Path(out)/"meta.json")
            self.assertTrue(1.9 <= float(inspect_video(result)) < 2.2)
            self.assertIn(("media.example.org", 80), connections)
            self.assertGreaterEqual(connections.count(("cdn.example.net", 80)), 3)
            self.assertTrue(json.loads((Path(out)/"meta.json").read_text())["title"])

    def test_dual_stack_docker_selects_the_private_ipv4_proxy_listener(self):
        from .extract import confine
        v4 = (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("172.16.1.2", 18322))
        with patch("socket.getaddrinfo", return_value=[v4]) as resolver, patch("sys.addaudithook"):
            self.assertEqual(confine("http://egress:18322", None), "http://172.16.1.2:18322")
        resolver.assert_called_once_with("egress", 18322, family=socket.AF_INET, type=socket.SOCK_STREAM)

    def test_collections_and_live_media_are_rejected_before_downloading(self):
        from .extract import MediaError, VideoDL
        with VideoDL({"quiet": True}, auto_init=False) as ydl:
            for result in ({"_type": "playlist", "entries": iter([{"id": "a"}, {"id": "b"}])},
                           {"is_live": True}, {"live_status": "is_upcoming"}):
                with self.assertRaises(MediaError):
                    ydl.process_ie_result(result)
