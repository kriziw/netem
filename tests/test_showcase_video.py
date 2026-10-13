"""The showcase's waiting screen can play a video: a YouTube or Vimeo link (embedded), or an uploaded MP4 or WebM file."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as netem
from showroom import create_showroom_app

MP4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 32
WEBM = b"\x1aE\xdf\xa3" + b"\x00" * 32


class ParseLinkTests(unittest.TestCase):
    def test_youtube_and_vimeo_links_become_their_embed_addresses(self):
        cases = {
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ": "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ",
            "youtu.be/dQw4w9WgXcQ?t=42": "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ",
            "https://www.youtube.com/shorts/dQw4w9WgXcQ": "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ",
            "https://www.youtube.com/embed/dQw4w9WgXcQ": "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ",
            "https://vimeo.com/76979871": "https://player.vimeo.com/video/76979871",
            "https://player.vimeo.com/video/76979871": "https://player.vimeo.com/video/76979871",
        }
        for link, embed in cases.items():
            with self.subTest(link=link):
                self.assertEqual(netem.parse_video_link(link)["src"], embed)

    def test_other_hosts_and_malformed_links_are_refused(self):
        for link in ("https://evil.example/watch?v=dQw4w9WgXcQ", "javascript:alert(1)", "file:///etc/passwd",
                     "https://www.youtube.com/watch?v=short", "https://vimeo.com/notanid", "", "   ",
                     "https://www.youtube.com/watch?v=" + "a" * 300):
            with self.subTest(link=link):
                with self.assertRaises(ValueError):
                    netem.parse_video_link(link)


class UploadCheckTests(unittest.TestCase):
    def test_only_real_mp4_webm_and_gif_animations_are_accepted(self):
        self.assertEqual(netem.check_video_upload("intro.MP4", MP4), ".mp4")
        self.assertEqual(netem.check_video_upload("intro.webm", WEBM), ".webm")
        self.assertEqual(netem.check_video_upload("loop.gif", b"GIF89a" + b"\x00" * 20), ".gif")
        with self.assertRaises(ValueError):
            netem.check_video_upload("loop.gif", MP4)
        for name, data in (("intro.mp4", WEBM), ("intro.webm", MP4), ("intro.gif", MP4), ("intro.html", b"<html>")):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    netem.check_video_upload(name, data)
        with self.assertRaises(ValueError):
            netem.check_video_upload("big.mp4", MP4 + b"\x00" * netem.SHOWCASE_VIDEO_MAX_BYTES)


class SaveAndPublishTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {}
        self.directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.directory.cleanup)
        for mock in (patch.object(netem, "load_config", side_effect=lambda: copy.deepcopy(self.cfg)),
                     patch.object(netem, "save_config", side_effect=self._save),
                     patch.object(netem, "log_event"),
                     patch.object(netem, "SHOWCASE_VIDEO_DIR", Path(self.directory.name) / "showcase")):
            mock.start()
            self.addCleanup(mock.stop)
        self.client = netem.app.test_client()
        with self.client.session_transaction() as state:
            state["integration_csrf"] = "token"

    def _save(self, cfg):
        # The real save replaces the stored configuration, so a removed key stays removed.
        self.cfg.clear()
        self.cfg.update(cfg)

    def test_a_link_is_saved_and_published_as_its_embed_only(self):
        self.client.post("/settings/showcase-video", data={"integration_csrf": "token",
                                                          "video_link": "https://youtu.be/dQw4w9WgXcQ"})
        self.assertEqual(self.cfg["showcase_video"]["kind"], "embed")
        self.assertEqual(netem.showcase_video_public(self.cfg),
                         {"kind": "embed", "provider": "youtube", "src": "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ"})

    def test_a_refused_link_leaves_the_setting_unchanged(self):
        self.cfg["showcase_video"] = {"kind": "embed", "provider": "vimeo", "src": "https://player.vimeo.com/video/76979871"}
        before = copy.deepcopy(self.cfg)
        self.client.post("/settings/showcase-video", data={"integration_csrf": "token", "video_link": "https://evil.example/x"})
        self.assertEqual(self.cfg, before)

    def test_an_uploaded_file_is_stored_and_served_by_the_showcase(self):
        from io import BytesIO
        self.client.post("/settings/showcase-video", data={"integration_csrf": "token",
                                                          "video_file": (BytesIO(MP4), "intro.mp4")},
                         content_type="multipart/form-data")
        self.assertEqual(self.cfg["showcase_video"]["kind"], "file")
        self.assertEqual(netem.showcase_video_public(self.cfg),
                         {"kind": "file", "src": "/assets/showcase-video", "media": "video"})
        self.assertTrue((Path(self.directory.name) / "showcase" / "video.mp4").is_file())

    def test_an_uploaded_animation_is_published_as_an_image(self):
        from io import BytesIO
        self.client.post("/settings/showcase-video", data={"integration_csrf": "token",
                                                          "video_file": (BytesIO(b"GIF89a" + b"\x00" * 20), "loop.gif")},
                         content_type="multipart/form-data")
        self.assertEqual(netem.showcase_video_public(self.cfg)["media"], "image")

    def test_removing_the_video_restores_the_animation(self):
        self.cfg["showcase_video"] = {"kind": "embed", "provider": "vimeo", "src": "https://player.vimeo.com/video/76979871"}
        self.client.post("/settings/showcase-video", data={"integration_csrf": "token", "remove": "1"})
        self.assertNotIn("showcase_video", self.cfg)
        self.assertIsNone(netem.showcase_video_public(self.cfg))

    def test_saving_requires_the_form_token(self):
        self.assertEqual(self.client.post("/settings/showcase-video", data={"video_link": "https://vimeo.com/76979871"}).status_code, 400)
        self.assertNotIn("showcase_video", self.cfg)

    def test_a_forged_embed_address_is_never_published(self):
        self.cfg["showcase_video"] = {"kind": "embed", "provider": "x", "src": "https://evil.example/embed/abc"}
        self.assertIsNone(netem.showcase_video_public(self.cfg))


class ShowcaseDeliveryTests(unittest.TestCase):
    def test_the_showcase_serves_the_uploaded_file_and_allows_only_the_video_hosts(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = Path(tmp) / "video.mp4"
            path.write_bytes(MP4)
            app = create_showroom_app(lambda: {}, video_file=lambda: path)
            client = app.test_client()
            response = client.get("/assets/showcase-video")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["Content-Type"], "video/mp4")
            self.assertIn("frame-src https://www.youtube-nocookie.com https://player.vimeo.com", response.headers["Content-Security-Policy"])
            response.close()
        absent = create_showroom_app(lambda: {}, video_file=lambda: None).test_client()
        self.assertEqual(absent.get("/assets/showcase-video").status_code, 404)

    def test_the_snapshot_publishes_the_video_before_and_during_a_session(self):
        self.cfg = {"showcase_video": {"kind": "embed", "provider": "youtube",
                                       "src": "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ"}}
        with patch.object(netem, "load_config", side_effect=lambda: copy.deepcopy(self.cfg)), \
             patch.object(netem, "session_snapshot", return_value={"active": False}):
            idle = netem.showroom_snapshot()
        self.assertEqual(idle["showcase_video"]["src"], "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ")
        with patch.object(netem, "load_config", side_effect=lambda: copy.deepcopy(self.cfg)), \
             patch.object(netem, "session_snapshot", return_value={"active": True, "started_at": 0, "name": "PoC"}), \
             patch.object(netem, "build_link_states", return_value=[]), \
             patch.object(netem, "current_diagnosis", return_value={}):
            live = netem.showroom_snapshot()
        self.assertEqual(live["showcase_video"]["provider"], "youtube")


if __name__ == "__main__":
    unittest.main()
