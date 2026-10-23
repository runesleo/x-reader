"""Platform fallback correctness: no empty XHS PASS and no Bilibili 412 dead end."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import requests

from x_reader.fetchers.bilibili import fetch_bilibili
from x_reader.fetchers.xhs import _valid_note, fetch_xhs
from x_reader.schema import from_bilibili
from x_reader.evidence import build_receipt


BILI_URL = "https://www.bilibili.com/video/BV1GXgY6GErB"
XHS_URL = "https://www.xiaohongshu.com/explore/69b15f20000000002603f539?xsec_token=TEST"

def http_error(status):
    response = requests.Response()
    response.status_code = status
    response.url = "https://api.bilibili.com/x/web-interface/view"
    return requests.HTTPError(f"{status} Client Error", response=response)


class BilibiliFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_api_success_propagates_media_unread(self):
        response = MagicMock()
        response.json.return_value = {
            "code": 0,
            "data": {"title": "Test episode", "desc": "Episode overview",
                     "owner": {"name": "Creator"}, "bvid": "BV1GXgY6GErB"}
        }
        with patch("x_reader.fetchers.bilibili.requests.get", return_value=response):
            video = await fetch_bilibili(BILI_URL)
        self.assertEqual(video["fetch_method"], "bilibili_api")
        result = from_bilibili(video)
        self.assertEqual(result.extra["media_status"], "present")
        self.assertFalse(result.extra["has_transcript"])

    async def test_412_browser_fallback_returns_partial_metadata(self):
        page = {
            "url": BILI_URL, "title": "",
            "content": "首页\n番剧\n投稿\n这才是企业级AI编程！Harness与Agent工程实践\n播放量\n视频选集\n" +
                       "如何搭建可靠的Agent工作流。\n" * 25,
            "author": "",
        }
        with patch("x_reader.fetchers.bilibili.requests.get", side_effect=http_error(412)):
            with patch("x_reader.fetchers.browser.fetch_via_browser", new_callable=AsyncMock, return_value=page):
                video = await fetch_bilibili(BILI_URL)
        self.assertIn("企业级AI编程", video["title"])
        self.assertEqual(video["fetch_method"], "bilibili_browser_fallback")
        self.assertEqual(video["media_status"], "present")
        self.assertFalse(video["has_transcript"])
        result = from_bilibili(video)
        self.assertEqual(result.extra["fetch_method"], "bilibili_browser_fallback")
        self.assertEqual(result.media_type.value, "video")
        receipt = build_receipt(result.to_dict())
        self.assertEqual(receipt["status"], "PARTIAL")
        self.assertEqual(receipt["components"]["spoken_media"], "PARTIAL")
        self.assertEqual(receipt["fetch_method"], "bilibili_browser_fallback")

    async def test_412_empty_browser_is_failure_not_success(self):
        page = {"url": BILI_URL, "title": "", "content": "首页 番剧 登录"}
        with patch("x_reader.fetchers.bilibili.requests.get", side_effect=http_error(412)):
            with patch("x_reader.fetchers.browser.fetch_via_browser", new_callable=AsyncMock, return_value=page):
                with self.assertRaisesRegex(RuntimeError, "no usable video metadata"):
                    await fetch_bilibili(BILI_URL)

    async def test_500_does_not_trigger_browser(self):
        with patch("x_reader.fetchers.bilibili.requests.get", side_effect=http_error(500)):
            with patch("x_reader.fetchers.browser.fetch_via_browser", new_callable=AsyncMock) as browser:
                with self.assertRaises(requests.HTTPError):
                    await fetch_bilibili(BILI_URL)
                browser.assert_not_called()


class XiaohongshuCorrectnessTests(unittest.IsolatedAsyncioTestCase):
    def test_rejects_404_and_empty_payload(self):
        self.assertFalse(_valid_note({"title":"", "content":""}, XHS_URL))
        self.assertFalse(_valid_note({"title":"Nice note", "content":"A real caption with useful text"},
                                     "https://www.xiaohongshu.com/404?error_code=300031"))
        self.assertFalse(_valid_note({"title":"安全限制", "content":"Account abnormal. Switch account and retry."}, XHS_URL))
        self.assertTrue(_valid_note({"title":"Valid note", "content":"This is the original note body."}, XHS_URL))

    async def test_jina_404_shell_rejected_without_session(self):
        page = {"title":"小红书 - 你访问的页面不见了",
                "content":"Sorry, This Page Isn't Available Right Now.", "url": XHS_URL}
        with patch("x_reader.fetchers.xhs.fetch_via_jina", return_value=page):
            with patch("x_reader.fetchers.browser.get_session_path", return_value="/nonexistent/xhs-benchmark.json"):
                with self.assertRaisesRegex(RuntimeError, "no saved session"):
                    await fetch_xhs(XHS_URL)

    async def test_browser_404_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp) / "xhs.json"
            session.write_text("{}", encoding="utf-8")
            with patch("x_reader.fetchers.xhs.fetch_via_jina", side_effect=RuntimeError("blocked")):
                with patch("x_reader.fetchers.browser.get_session_path", return_value=str(session)):
                    with patch("x_reader.fetchers.browser.fetch_via_browser", new_callable=AsyncMock,
                               return_value={"title":"", "content":"", "url":"https://www.xiaohongshu.com/404?error_code=300031"}):
                        with self.assertRaisesRegex(RuntimeError, "blocked or empty"):
                            await fetch_xhs(XHS_URL)

    async def test_browser_empty_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp) / "xhs.json"
            session.write_text("{}", encoding="utf-8")
            with patch("x_reader.fetchers.xhs.fetch_via_jina", side_effect=RuntimeError("blocked")):
                with patch("x_reader.fetchers.browser.get_session_path", return_value=str(session)):
                    with patch("x_reader.fetchers.browser.fetch_via_browser", new_callable=AsyncMock,
                               return_value={"title":"", "content":"", "url":XHS_URL}):
                        with self.assertRaisesRegex(RuntimeError, "blocked or empty"):
                            await fetch_xhs(XHS_URL)

    async def test_browser_valid_note_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp) / "xhs.json"
            session.write_text("{}", encoding="utf-8")
            page = {"title":"Useful public note", "content":"Useful note content, over minimum threshold.", "url": XHS_URL}
            with patch("x_reader.fetchers.xhs.fetch_via_jina", side_effect=RuntimeError("blocked")):
                with patch("x_reader.fetchers.browser.get_session_path", return_value=str(session)):
                    with patch("x_reader.fetchers.browser.fetch_via_browser", new_callable=AsyncMock, return_value=page):
                        note = await fetch_xhs(XHS_URL)
        self.assertEqual(note["title"], "Useful public note")
        self.assertIn("Useful note content", note["content"])


if __name__ == "__main__":
    unittest.main()
