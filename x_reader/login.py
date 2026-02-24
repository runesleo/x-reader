# -*- coding: utf-8 -*-
"""
Login manager — opens a visible browser for manual login, saves session.

Usage:
    x-reader login xhs       # Login to Xiaohongshu
    x-reader login wechat     # Login to WeChat (if needed)

Sessions are saved as Playwright storage_state JSON files.
"""

from pathlib import Path
from loguru import logger

SESSION_DIR = Path.home() / ".x-reader" / "sessions"

PLATFORM_URLS = {
    "xhs": "https://www.xiaohongshu.com/explore",
    "xiaohongshu": "https://www.xiaohongshu.com/explore",
    "wechat": "https://mp.weixin.qq.com",
    "twitter": "https://x.com/login",
    "x": "https://x.com/login",
}


def login(platform: str) -> None:
    """
    Open a visible browser for the user to log in manually.
    After login, saves cookies/localStorage to a session file.

    Args:
        platform: Platform key (e.g. 'xhs', 'wechat')
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            "❌ Playwright is not installed. Run:\n"
            '   pip install "x-reader[browser]"\n'
            "   playwright install chromium"
        )
        return

    platform = platform.lower()
    login_url = PLATFORM_URLS.get(platform)
    if not login_url:
        supported = ", ".join(sorted(PLATFORM_URLS.keys()))
        print(f"❌ Unknown platform: {platform}")
        print(f"   Supported: {supported}")
        return

    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    session_path = SESSION_DIR / f"{platform}.json"
    # Normalize alias to canonical name
    if platform in ("xhs", "xiaohongshu"):
        canonical = "xhs"
    elif platform in ("twitter", "x"):
        canonical = "twitter"
    else:
        canonical = platform
    session_path = SESSION_DIR / f"{canonical}.json"

    print(f"🌐 Opening {platform} login page: {login_url}")
    print("   Please log in manually in the browser window.")
    print("   When done, close the browser or press Ctrl+C.\n")

    with sync_playwright() as p:
        # Prefer real Chrome channel over bundled Chromium to reduce login friction.
        browser = p.chromium.launch(
            headless=False,
            channel="chrome",
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36",
        )
        page = context.new_page()
        page.goto(login_url)

        try:
            # Wait for user to log in — blocks until browser is closed
            page.wait_for_event("close", timeout=300_000)  # 5 min max
        except KeyboardInterrupt:
            pass
        except Exception:
            pass  # Browser closed by user

        # Save session regardless of how we got here
        context.storage_state(path=str(session_path))
        logger.info(f"Session saved: {session_path}")
        print(f"\n✅ Session saved to {session_path}")

        context.close()
        browser.close()
