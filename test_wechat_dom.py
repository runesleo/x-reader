import asyncio
from playwright.async_api import async_playwright
import string

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        # A random article URL from mp.weixin.qq.com
        url = "https://mp.weixin.qq.com/s/72D0jSow0x1VONvIqSmsuw"
        await page.goto(url, wait_until="domcontentloaded", timeout=15000)
        await page.wait_for_timeout(2000)

        # Safe extraction
        content = await page.evaluate("""() => {
            const container = document.querySelector('#js_content');
            if (!container) return 'No #js_content found';

            let elements = [];
            // Parse through all immediate children or relevant tags
            const walk = (node) => {
                if (node.tagName === 'IMG') {
                   let src = node.getAttribute('data-src') || node.getAttribute('src');
                   if (src) {
                       // Replace mmbiz.qpic.cn with wsrv proxy if needed
                       elements.push(`![image](${src})`);
                   }
                } else if (node.nodeType === Node.TEXT_NODE && node.textContent.trim()) {
                   elements.push(node.textContent.trim());
                } else {
                   for (let child of node.childNodes) {
                       walk(child);
                   }
                }
            };
            walk(container);
            return elements.join('\\n\\n');
        }""")
        print("Extracted Length:", len(content))
        print("Snippet:", content[:500])

        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
