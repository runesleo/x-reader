# -*- coding: utf-8 -*-
"""
Login manager — opens a browser for manual login, saves session.

Usage:
    x-reader login xhs              # Visible browser login
    x-reader login xhs --headless   # Headless: saves QR screenshot for scanning

Sessions are saved as Playwright storage_state JSON files.
"""

import os
import time
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


def _save_session(context, session_path: Path) -> None:
    """Save session and set restrictive permissions."""
    context.storage_state(path=str(session_path))
    os.chmod(str(session_path), 0o600)
    logger.info(f"Session saved: {session_path}")
    print(f"\n✅ Session saved to {session_path}")


def _resolve_canonical(platform: str) -> str:
    if platform in ("xhs", "xiaohongshu"):
        return "xhs"
    if platform in ("twitter", "x"):
        return "twitter"
    return platform


def login(platform: str, headless: bool = False) -> None:
    """
    Open a browser for the user to log in manually.
    After login, saves cookies/localStorage to a session file.

    Args:
        platform: Platform key (e.g. 'xhs', 'wechat')
        headless: If True, run headless and save QR screenshot for user to scan
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
    canonical = _resolve_canonical(platform)
    session_path = SESSION_DIR / f"{canonical}.json"

    if headless:
        _login_headless(login_url, session_path, canonical)
    else:
        _login_visible(login_url, session_path, platform)


def _login_visible(login_url: str, session_path: Path, platform: str) -> None:
    from playwright.sync_api import sync_playwright

    print(f"🌐 Opening {platform} login page: {login_url}")
    print("   Please log in manually in the browser window.")
    print("   When done, close the browser or press Ctrl+C.\n")

    with sync_playwright() as p:
        # Prefer real Chrome, fall back to bundled Chromium (e.g. Linux ARM64).
        launch_args = dict(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            browser = p.chromium.launch(channel="chrome", **launch_args)
        except Exception:
            browser = p.chromium.launch(**launch_args)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36",
        )
        page = context.new_page()
        page.goto(login_url)

        try:
            page.wait_for_event("close", timeout=300_000)
        except KeyboardInterrupt:
            pass
        except Exception:
            pass

        _save_session(context, session_path)
        context.close()
        browser.close()


def _login_headless(login_url: str, session_path: Path, canonical: str) -> None:
    from playwright.sync_api import sync_playwright

    qr_path = SESSION_DIR / f"{canonical}_qr.png"

    print(f"🔐 Headless login: {login_url}")
    print(f"   QR code will be saved to: {qr_path}")
    print("   Waiting for login (timeout: 5 min)...\n")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36",
        )
        page = context.new_page()
        page.goto(login_url, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)

        # Save QR screenshot
        page.screenshot(path=str(qr_path))
        print(f"📸 QR screenshot saved: {qr_path}")
        print("   Open this image and scan the QR code with your phone.\n")

        # Poll for cookie change (login detection)
        initial_cookies = len(context.cookies())
        timeout = 300  # 5 min
        start = time.time()
        logged_in = False

        try:
            while time.time() - start < timeout:
                time.sleep(3)
                current_cookies = len(context.cookies())
                if current_cookies > initial_cookies + 2:
                    logger.info(f"Cookie count changed: {initial_cookies} -> {current_cookies}")
                    # Wait a bit more for all cookies to settle
                    page.wait_for_timeout(2000)
                    logged_in = True
                    break
        except KeyboardInterrupt:
            pass

        if logged_in:
            _save_session(context, session_path)
        else:
            print("\n⏹ Login timed out or cancelled. No session saved.")

        context.close()
        browser.close()
