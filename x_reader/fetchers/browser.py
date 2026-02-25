# -*- coding: utf-8 -*-
"""
Playwright browser fetcher — headless Chromium fallback for anti-scraping sites.

Used when Jina Reader fails (403/451/timeout). Supports persistent login
sessions via Playwright's storage_state for platforms requiring authentication.

Install: pip install "x-reader[browser]" && playwright install chromium
"""

from loguru import logger
import os
from pathlib import Path

SESSION_DIR = Path.home() / ".x-reader" / "sessions"
TIMEOUT_MS = 30_000


async def fetch_via_browser(url: str, storage_state: str = None) -> dict:
    """
    Fetch a URL using headless Chromium via Playwright.

    Args:
        url: Target URL to fetch.
        storage_state: Path to a Playwright storage state JSON file (cookies/localStorage).
                       If provided, the browser context will load this session.

    Returns:
        dict with keys: title, content, url, author
    """
    # Security: Validate URL before fetching
    from x_reader.utils.url_validator import validate_url
    validate_url(url)

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise RuntimeError(
            "Playwright is not installed. Run:\n"
            '  pip install "x-reader[browser]"\n'
            "  playwright install chromium"
        )

    logger.info(f"Browser fetch: {url}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        context_kwargs = {}
        if storage_state and Path(storage_state).exists():
            # Security: Validate session file permissions (should be 0o600)
            session_stat = os.stat(storage_state)
            session_mode = session_stat.st_mode & 0o777
            if session_mode & 0o077:
                logger.warning(
                    f"Session file {storage_state} has insecure permissions {oct(session_mode)}. "
                    "Should be 0o600. Fixing..."
                )
                os.chmod(storage_state, 0o600)

            context_kwargs["storage_state"] = storage_state
            logger.info(f"Using session: {storage_state}")

        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36",
            **context_kwargs,
        )
        page = await context.new_page()

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT_MS)

            is_xhs = "xiaohongshu.com" in url or "xhslink.com" in url

            if is_xhs:
                # XHS SPA needs the note container to render
                try:
                    await page.wait_for_selector("#noteContainer", timeout=8000)
                except Exception:
                    logger.warning("[XHS] #noteContainer not found within 8s, proceeding anyway")
                await page.wait_for_timeout(1000)

                data = await page.evaluate("""() => {
                    const title = document.querySelector('#detail-title');
                    const desc = document.querySelector('#detail-desc');
                    const meta = document.querySelector('.bottom-container');
                    const author = document.querySelector('.author-wrapper .username')
                        || document.querySelector('.interaction-container');
                    return {
                        title: title ? title.innerText.trim() : '',
                        content: [
                            desc ? desc.innerText.trim() : '',
                            meta ? meta.innerText.trim() : '',
                        ].filter(Boolean).join('\\n\\n'),
                        author: author ? author.innerText.trim().split('\\n')[0] : '',
                    };
                }""")

                result = {
                    "title": (data["title"] or "").strip()[:200],
                    "content": (data["content"] or "").strip(),
                    "url": page.url,
                    "author": (data["author"] or "").strip(),
                }
            else:
                # Generic fallback for non-XHS pages
                await page.wait_for_timeout(2000)

                title = await page.title()
                content = await page.evaluate("""() => {
                    const el = document.querySelector('article')
                        || document.querySelector('main')
                        || document.querySelector('.content')
                        || document.body;
                    return el ? el.innerText : '';
                }""")

                result = {
                    "title": (title or "").strip()[:200],
                    "content": (content or "").strip(),
                    "url": page.url,
                    "author": "",
                }

            logger.info(f"Browser fetch OK: {result['title'][:60]}")
            return result

        finally:
            await context.close()
            await browser.close()


def get_session_path(platform: str) -> str:
    """Get the session file path for a platform."""
    return str(SESSION_DIR / f"{platform}.json")
