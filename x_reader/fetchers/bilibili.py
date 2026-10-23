# -*- coding: utf-8 -*-
"""Bilibili video metadata fetcher with an explicit browser fallback for API 412.

A video-page description is not a transcript. Neither API nor browser
metadata proves that the video's audio/visual content was consumed.
"""

import asyncio
import re
from typing import Any, Dict

import requests
from loguru import logger


API_URL = "https://api.bilibili.com/x/web-interface/view"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
}


def _page_title(page: dict) -> str:
    title = str(page.get("title") or "").strip()
    if title and "哔哩哔哩" in title and len(title) > 12:
        return title.split("_哔哩哔哩", 1)[0].strip()

    # Bilibili's browser body can contain the video title immediately after
    # the '投稿' navigation item even when document.title is empty.
    text = str(page.get("content") or "")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for i, line in enumerate(lines):
        if line == "投稿" and i + 1 < len(lines):
            candidate = lines[i + 1]
            if len(candidate) >= 12 and not candidate.startswith(("首页", "番剧", "登录")):
                return candidate[:200]
    return ""


async def _browser_fallback(url: str, bv_id: str) -> Dict[str, Any]:
    from x_reader.fetchers.browser import fetch_via_browser

    page = await fetch_via_browser(url)
    content = str(page.get("content") or "").strip()
    title = _page_title(page)
    final = str(page.get("url") or "")
    # We require substantive video-page features before treating the
    # browser response as source metadata. A 412/login/error shell is not data.
    has_video_page = ("视频选集" in content or "播放量" in content or "UP主" in content)
    if (
        not title
        or len(content) < 120
        or not has_video_page
        or "/404" in final.split("?", 1)[0]
        or "安全验证" in content[:300]
    ):
        raise RuntimeError("Bilibili browser fallback returned no usable video metadata")

    return {
        "title": title,
        "description": content[:20000],
        "author": str(page.get("author") or ""),
        "url": f"https://www.bilibili.com/video/{bv_id}",
        "cover": "",
        "bvid": bv_id,
        "duration": 0,
        "view_count": 0,
        "platform": "bilibili",
        "fetch_method": "bilibili_browser_fallback",
        "media_status": "present",
        "has_transcript": False,
    }


async def fetch_bilibili(url_or_bv: str) -> Dict[str, Any]:
    """Fetch source metadata; use isolated browser if public API returns 412."""
    logger.info(f"Fetching Bilibili: {url_or_bv}")
    match = re.search(r"BV[A-Za-z0-9]+", url_or_bv)
    if not match:
        raise ValueError(f"Cannot extract BV ID from: {url_or_bv}")
    bv_id = match.group()
    url = f"https://www.bilibili.com/video/{bv_id}"

    try:
        resp = await asyncio.to_thread(
            requests.get, API_URL, params={"bvid": bv_id},
            headers=HEADERS, timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.HTTPError as exc:
        if exc.response is None or exc.response.status_code != 412:
            raise
        logger.warning(f"Bilibili API 412 for {bv_id}; trying browser fallback")
        return await _browser_fallback(url, bv_id)

    if data.get("code") == -412:
        return await _browser_fallback(url, bv_id)
    if data.get("code") != 0:
        raise ValueError(f"Bilibili API error: {data.get('message')}")

    video = data["data"]
    return {
        "title": video.get("title", ""),
        "description": video.get("desc", ""),
        "author": video.get("owner", {}).get("name", ""),
        "url": url,
        "cover": video.get("pic", ""),
        "bvid": bv_id,
        "duration": video.get("duration", 0),
        "view_count": video.get("stat", {}).get("view", 0),
        "platform": "bilibili",
        "fetch_method": "bilibili_api",
        "media_status": "present",
        "has_transcript": False,
    }
