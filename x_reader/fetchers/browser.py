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

# A WeChat article as the browser lays it out: the browser's own innerText of
# the article (inline runs join, blocks and <br> part lines, display:none and
# visibility:hidden text left out), with the images it shows kept in place.
# Players, frames, plugins and WeChat's own media components carry content
# this read does not take: they are counted and set aside. What innerText
# cannot vouch for is reported as risks: text too small or faint to read,
# turned, clipped or off the page, shown in another order, text made by CSS,
# and anything outside the article painted over it. The page's whole text is
# returned too, for walls drawn outside the article. The page is left as it
# was found (the temporary marking uses an attribute name no page can know).
_WECHAT_ARTICLE_JS = r"""() => {
    const container = document.querySelector('#js_content') || document.querySelector('.rich_media_content');
    if (!container) return null; // Safe fallback to generic if not found

    const embedded = /^(IFRAME|VIDEO|AUDIO|OBJECT|EMBED|CANVAS|MPVOICE|MPVIDEO|QQMUSIC|MP-.+)$/;
    const mark = 'data-xr-' + Math.random().toString(36).slice(2) + Date.now().toString(36);
    const images = [];
    const markers = [];
    const skipped = [];
    let embeds = 0;
    const walk = (element) => {
        for (const child of element.children) {
            if (embedded.test(child.tagName)) {
                embeds += 1;
                child.setAttribute(mark, '');
                skipped.push(child);
            } else if (child.tagName === 'IMG') {
                const src = child.getAttribute('data-src') || child.getAttribute('src');
                if (src && child.getClientRects().length && getComputedStyle(child).visibility === 'visible') {
                    const marker = document.createTextNode(`\u0001${images.length}\u0001`);
                    images.push(src);
                    child.after(marker);
                    markers.push(marker);
                }
            } else {
                walk(child);
            }
        }
    };
    walk(container);
    const style = document.createElement('style');
    style.textContent = `[${mark}] { display: none !important; } * { pointer-events: auto !important; }`;
    (document.head || document.documentElement).append(style);
    const risks = new Set();
    let text;
    try {
        text = container.innerText;
        // (innerText of an element that is not rendered is its raw text)
        if (!container.getClientRects().length || getComputedStyle(container).visibility !== 'visible') {
            risks.add('unrendered_article');
        }
        const alpha = (color) => {
            const parts = (color.match(/[\d.]+/g) || []).map(Number);
            return color.startsWith('rgba') || color.includes('/') ? (parts.length > 3 ? parts[3] : 1) : 1;
        };
        const luminance = (color) => {
            const [r, g, b] = (color.match(/[\d.]+/g) || [0, 0, 0]).map(Number).map(value => {
                const c = value / 255;
                return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
            });
            return 0.2126 * r + 0.7152 * g + 0.0722 * b;
        };
        const backdrop = (element) => {
            for (let node = element; node && node.nodeType === 1; node = node.parentElement) {
                const own = getComputedStyle(node);
                if (own.backgroundImage !== 'none') return null; // an image behind the text: not judged
                if (alpha(own.backgroundColor) > 0.5) return own.backgroundColor;
            }
            return 'rgb(255, 255, 255)';
        };
        const pageWidth = Math.max(document.documentElement.scrollWidth, window.innerWidth);
        const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
        let checked = 0;
        const placed = new Set(markers);
        for (let node = walker.nextNode(); node; node = walker.nextNode()) {
            if (!node.data.trim() || placed.has(node)) continue;
            const element = node.parentElement;
            if (!element || element.closest(`[${mark}]`)) continue;
            const own = getComputedStyle(element);
            if (own.visibility !== 'visible') continue; // left out of innerText
            const range = document.createRange();
            range.selectNodeContents(node);
            const boxes = [...range.getClientRects()];
            if (!boxes.length) {
                // not laid out: left out of innerText under display:none, otherwise read but not drawn
                if (!element.checkVisibility || element.checkVisibility()) risks.add('unrendered_text');
                continue;
            }
            if (++checked > 20000) { risks.add('too_much_to_check'); break; }
            const drawn = boxes.filter(box => box.width > 0.5 && box.height > 0.5);
            if (!drawn.length || parseFloat(own.fontSize) < 6) risks.add('small_text');
            let opacity = 1;
            for (let node2 = element; node2 && node2.nodeType === 1; node2 = node2.parentElement) {
                const up = getComputedStyle(node2);
                opacity *= parseFloat(up.opacity);
                if (up.transform !== 'none') {
                    const m = up.transform.match(/matrix\(([^)]+)\)/);
                    const [a, b, c, d] = m ? m[1].split(',').map(Number) : [0, 0, 0, 0];
                    const turned = Math.abs(Math.atan2(b, a)) > Math.PI / 4;
                    if (!m || a * d - b * c < 0.25 || turned) risks.add('transformed_text');
                }
                if (up.clipPath !== 'none' || (up.clip && up.clip !== 'auto')) risks.add('clipped_text');
                if (up.filter && up.filter !== 'none') risks.add('filtered_text');
                if (up.overflowX !== 'visible' || up.overflowY !== 'visible') {
                    const frame = node2.getBoundingClientRect();
                    if (drawn.some(box => box.left < frame.left - 2 || box.right > frame.right + 2
                            || box.top < frame.top - 2 || box.bottom > frame.bottom + 2)) risks.add('clipped_text');
                }
                if (node2 !== container && node2.parentElement) {
                    const holder = getComputedStyle(node2.parentElement);
                    if (/flex|grid/.test(holder.display) && (up.order !== '0' || /reverse/.test(holder.flexDirection)
                            || holder.flexWrap === 'wrap-reverse')) risks.add('reordered_text');
                }
                if (node2 === document.body) break;
            }
            const fill = own.webkitTextFillColor || own.color;  // (the colour the letters are filled with)
            if (opacity < 0.3 || alpha(own.color) < 0.3 || alpha(fill) < 0.3) {
                risks.add('faint_text');
            } else {
                const behind = backdrop(element);
                if (behind) {
                    const [light, dark] = [luminance(fill), luminance(behind)].sort((x, y) => y - x);
                    if ((light + 0.05) / (dark + 0.05) < 1.3) risks.add('faint_text');
                }
            }
            if (own.direction === 'rtl' || /override|plaintext/.test(own.unicodeBidi)) risks.add('reordered_text');
            if (drawn.some(box => box.right + window.scrollX < 0 || box.bottom + window.scrollY < 0
                    || box.left + window.scrollX > pageWidth)) risks.add('offscreen_text');
        }
        if (/[‪-‮⁦-⁩]/.test(text)) risks.add('reordered_text');
        for (const element of [container, ...container.querySelectorAll('*')]) {
            if (element.closest(`[${mark}]`)) continue;
            for (const pseudo of ['::before', '::after', '::marker']) {
                const made = getComputedStyle(element, pseudo).content;  // (strings, attr(), counters...)
                if (made && !['none', 'normal', '""', "''"].includes(made.trim())) risks.add('generated_text');
            }
        }
        // paint hit testing does not see: wide outlines and shadows anywhere, boxes of positioned pseudo-elements
        // (rendered, visible and nearly opaque paint, large enough to hide a line)
        for (const element of document.querySelectorAll('*')) {
            if (!element.getClientRects().length) continue;
            const own = getComputedStyle(element);
            if (own.visibility !== 'visible') continue;
            if (own.outlineStyle !== 'none' && parseFloat(own.outlineWidth) > 3 && alpha(own.outlineColor) > 0.5) {
                risks.add('covered_text');
            }
            if (own.boxShadow !== 'none' && own.boxShadow.split(/,(?![^(]*\))/).some(shadow => !/inset/.test(shadow)
                    && alpha((shadow.match(/rgba?\([^)]*\)/) || ['rgb(0, 0, 0)'])[0]) > 0.5
                    && (shadow.replace(/rgba?\([^)]*\)/g, '').match(/-?[\d.]+px/g) || [])
                        .some(length => Math.abs(parseFloat(length)) > 16))) risks.add('covered_text');
            for (const pseudo of ['::before', '::after']) {
                const made = getComputedStyle(element, pseudo);
                if (!['none', 'normal'].includes(made.content) && /absolute|fixed/.test(made.position)
                        && made.visibility === 'visible'
                        && (alpha(made.backgroundColor) > 0.5 || made.backgroundImage !== 'none')
                        && !(parseFloat(made.width) < 50 || parseFloat(made.height) < 30)) {
                    risks.add('covered_text');
                }
            }
        }
        // anything painted over the text: at the middle of each piece of text, page by page
        // (a band of each screen, so bars fixed to its edges do not count)
        const startY = window.scrollY;
        const spots = [];
        const walker2 = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
        for (let node = walker2.nextNode(); node && spots.length < 5000; node = walker2.nextNode()) {
            if (!node.data.trim() || placed.has(node) || !node.parentElement
                    || node.parentElement.closest(`[${mark}]`)) continue;
            const range = document.createRange();
            range.selectNodeContents(node);
            for (const box of range.getClientRects()) {
                if (box.width > 1 && box.height > 1) {
                    spots.push([node.parentElement, box.left + box.width / 2, box.top + box.height / 2 + startY]);
                }
            }
        }
        const band = window.innerHeight * 0.7;
        const height = Math.max(document.documentElement.scrollHeight, document.body ? document.body.scrollHeight : 0);
        const last = Math.max(0, height - window.innerHeight);
        try {
            for (let y = 0; !risks.has('covered_text'); y = Math.min(y + band, last)) {
                window.scrollTo(0, y);
                const shift = window.scrollY;
                // (the screen's edges count where the page cannot scroll on past them)
                const low = shift <= 1 ? 0 : window.innerHeight * 0.15;
                const high = shift >= last - 1 ? window.innerHeight : window.innerHeight * 0.85;
                for (const [owner, x, spotY] of spots) {
                    const at = spotY - shift;
                    if (at < low || at > high || x < 0 || x > window.innerWidth) continue;
                    const hit = document.elementFromPoint(x, at);
                    if (hit && hit !== owner && !owner.contains(hit) && !hit.contains(owner)) {
                        risks.add('covered_text');
                        break;
                    }
                }
                if (y >= last) break;
            }
        } finally {
            window.scrollTo(0, startY);
        }
    } finally {
        style.remove();
        skipped.forEach(element => element.removeAttribute(mark));
        markers.forEach(marker => marker.remove());
    }
    const content = text
        .replace(/\u0001(\d+)\u0001/g, (_match, index) => `\n\n![image](${images[index]})\n\n`)
        .split('\n').map(line => line.replace(/[ \t ]+/g, ' ').trim()).join('\n')
        .replace(/\n{3,}/g, '\n\n').trim();
    const whole = document.body ? document.body.innerText : '';
    return {content: content, embeds: embeds, risks: [...risks].sort(), page_text: whole.slice(0, 300000),
            page_text_complete: whole.length <= 300000};
}"""


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
            is_wechat = "mp.weixin.qq.com" in url

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
            elif is_wechat:
                # WeChat Public Accounts specific extraction to preserve images
                await page.wait_for_timeout(2000)

                title = await page.title()
                walked = await page.evaluate(_WECHAT_ARTICLE_JS)
                content = walked["content"] if walked else None

                # Generic fallback if WeChat specific extraction yields nothing;
                # recorded so the receipt does not treat page chrome as the article.
                extraction = "container" if content else "page_body"
                if not content:
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
                    "extraction": extraction,
                }
                if walked and extraction == "container":
                    result["embedded_media"] = int(walked.get("embeds") or 0)
                    result["visual_risks"] = [str(risk) for risk in walked.get("risks") or []]
                    result["page_text"] = str(walked.get("page_text") or "")
                    result["page_text_complete"] = walked.get("page_text_complete") is True
            else:
                # Generic fallback: a Bilibili page may navigate after the
                # initial HTTP 412 response. Allow DOM stabilization and retry
                # extraction only for navigation-context races.
                is_bilibili = "bilibili.com" in url or "b23.tv" in url
                await page.wait_for_timeout(3500 if is_bilibili else 2000)
                for attempt in range(3):
                    try:
                        title = await page.title()
                        content = await page.evaluate("""() => {
                            const el = document.querySelector('article')
                                || document.querySelector('main')
                                || document.querySelector('.content')
                                || document.body;
                            return el ? el.innerText : '';
                        }""")
                        break
                    except Exception as exc:
                        message = str(exc).lower()
                        if attempt == 2 or not any(word in message for word in (
                            "execution context was destroyed", "navigation", "frame was detached"
                        )):
                            raise
                        logger.warning("Browser page navigated during extraction; retrying")
                        await page.wait_for_timeout(1200)

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
