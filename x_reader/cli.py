# -*- coding: utf-8 -*-
"""
x-reader CLI — fetch content from any platform.

Usage:
    x-reader <url>                     # Fetch a single URL
    x-reader <url1> <url2> ...         # Fetch multiple URLs
    x-reader <url> --json              # Full machine-readable payload
    x-reader doctor [--json]           # Read-only self-check: what is ready, what is missing
    x-reader list                      # Show inbox contents
    x-reader clear                     # Clear inbox
"""

import sys
import asyncio
import json
from pathlib import Path

try:
    import os as _os
    from dotenv import load_dotenv
    if _os.environ.get("X_READER_HOSTED") != "1":  # hosted reads must not pick up local secrets
        load_dotenv()

    from x_reader.reader import UniversalReader
    from x_reader.schema import UnifiedInbox, SourceType
    _IMPORT_ERROR = None
except ImportError as exc:  # keep `x-reader doctor` usable on a broken install
    UniversalReader = UnifiedInbox = SourceType = None
    _IMPORT_ERROR = exc


def get_inbox_path() -> str:
    import os
    return os.getenv("INBOX_FILE", "unified_inbox.json")


def cmd_fetch(urls: list[str], json_output: bool = False, media_preview_seconds: int = 0,
              full_short_audio: bool = False, full_long_audio: bool = False):
    """Fetch one or more URLs.

    When json_output is True, stdout is machine-readable JSON containing the
    complete UnifiedContent payload instead of the human preview.
    """
    import os
    inbox = None if os.environ.get("X_READER_HOSTED") == "1" else UnifiedInbox(get_inbox_path())
    reader = UniversalReader(
        inbox=inbox, media_preview_seconds=media_preview_seconds,
        full_short_audio=full_short_audio, full_long_audio=full_long_audio,
    )

    async def run():
        if len(urls) == 1:
            item = await reader.read(urls[0])
            if json_output:
                print(json.dumps(item.to_dict(), ensure_ascii=False, indent=2))
            else:
                print(f"✅ [{item.source_type.value}] {item.title[:60]}")
                print(f"   {item.url}")
                print(f"   {item.content[:200]}...")
        else:
            items = await reader.read_batch(urls)
            if json_output:
                print(json.dumps(
                    [item.to_dict() for item in items],
                    ensure_ascii=False,
                    indent=2,
                ))
            else:
                for item in items:
                    print(f"✅ [{item.source_type.value}] {item.title[:60]}")
                print(f"\n📦 Fetched {len(items)}/{len(urls)} URLs")

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        if json_output:
            print(json.dumps({"ok": False, "error": "cancelled"}, ensure_ascii=False))
        else:
            print("\n⏹ Cancelled")
        sys.exit(130)
    except Exception as e:
        if json_output:
            print(json.dumps(
                {"ok": False, "error": str(e), "error_type": type(e).__name__},
                ensure_ascii=False,
            ))
        else:
            print(f"❌ {e}")
        sys.exit(1)


def cmd_list():
    """Show inbox contents."""
    inbox = UnifiedInbox(get_inbox_path())
    if not inbox.items:
        print("📦 Inbox is empty")
        return

    print(f"📦 Inbox: {len(inbox.items)} items\n")

    emoji_map = {
        SourceType.TELEGRAM: "📢", SourceType.RSS: "📰",
        SourceType.BILIBILI: "🎬", SourceType.PODCAST: "🎧", SourceType.XIAOHONGSHU: "📕",
        SourceType.TWITTER: "🐦", SourceType.WECHAT: "💬",
        SourceType.YOUTUBE: "▶️", SourceType.MANUAL: "✏️",
    }

    for i, item in enumerate(inbox.items[-20:], 1):
        emoji = emoji_map.get(item.source_type, "📄")
        print(f"  {i:2d}. {emoji} [{item.source_type.value:8s}] {item.title[:50]}")


def cmd_clear():
    """Clear inbox."""
    path = Path(get_inbox_path())
    if path.exists():
        confirm = input("Clear inbox? (y/N) ")
        if confirm.lower() == 'y':
            path.write_text("[]")
            print("✅ Inbox cleared")
    else:
        print("📦 Inbox is already empty")


def cmd_login(platform: str, headless: bool = False):
    """Open browser for manual login to a platform."""
    from x_reader.login import login
    login(platform, headless=headless)


def parse_media_preview_option(args: list[str]) -> tuple[list[str], int]:
    """Only explicit --media-preview-seconds N enables bounded audio work."""
    if "--media-preview-seconds" not in args:
        return args, 0
    if args.count("--media-preview-seconds") != 1:
        raise ValueError("Specify --media-preview-seconds at most once")
    index = args.index("--media-preview-seconds")
    if index + 1 >= len(args):
        raise ValueError("Missing number after --media-preview-seconds")
    try:
        seconds = int(args[index + 1])
    except ValueError as exc:
        raise ValueError("Media preview seconds must be an integer") from exc
    if not 1 <= seconds <= 30:
        raise ValueError("Media preview must be between 1 and 30 seconds")
    return args[:index] + args[index + 2:], seconds


def main():
    if len(sys.argv) < 2:
        print("""
📖 x-reader — Universal content reader

Usage:
    x-reader <url>              Fetch content from any URL
    x-reader <url1> <url2>      Fetch multiple URLs
    x-reader <url> --json       Print complete UnifiedContent as JSON
    x-reader <podcast-url> --media-preview-seconds 12 --json
                                Opt-in local ASR of 1-30s; always PARTIAL
    x-reader <podcast-url> --media-full-short --json
                                Opt-in whole-source ASR only if <=2 MiB / <=120s
    x-reader <podcast-url> --media-full-long --json
                                Opt-in resumable whole-source <=64 MiB / <=90m
    x-reader login <platform>   Login to a platform (saves session for browser fallback)
    x-reader doctor [--json]    Check what is ready and what is missing (read-only)
    x-reader list               Show inbox contents
    x-reader clear              Clear inbox

Supported platforms:
    WeChat, Telegram, X/Twitter, YouTube,
    Bilibili, podcasts, Xiaohongshu, RSS, and any web page

Examples:
    x-reader https://mp.weixin.qq.com/s/abc123
    x-reader https://x.com/elonmusk/status/123456
    x-reader https://www.xiaohongshu.com/explore/abc123
    x-reader login xhs
""")
        return

    args = sys.argv[1:]
    json_output = "--json" in args
    args = [arg for arg in args if arg != "--json"]

    def usage_error(message: str, code: int = 2):
        # With --json, stdout stays machine-readable even for usage errors.
        if json_output:
            print(json.dumps({"ok": False, "error": message, "error_type": "UsageError"}, ensure_ascii=False))
        else:
            print(f"❌ {message}")
        sys.exit(code)

    try:
        if args.count("--media-full-short") > 1 or args.count("--media-full-long") > 1:
            raise ValueError("Specify each full-media flag at most once")
        full_short_audio = "--media-full-short" in args
        full_long_audio = "--media-full-long" in args
        args = [arg for arg in args if arg not in ("--media-full-short", "--media-full-long")]
        args, media_preview_seconds = parse_media_preview_option(args)
        if sum((full_short_audio, full_long_audio, bool(media_preview_seconds))) > 1:
            raise ValueError("Full-long, full-short and preview flags are mutually exclusive")
    except ValueError as exc:
        usage_error(str(exc))
    if not args:
        usage_error("Missing URL or command", code=1)

    cmd = args[0].lower()

    if cmd == "doctor":
        from x_reader.doctor import run_doctor
        sys.exit(run_doctor(json_output=json_output))

    if _IMPORT_ERROR is not None:
        message = f"x-reader is missing a dependency ({_IMPORT_ERROR}). Run `x-reader doctor` to see what to fix."
        if json_output:
            print(json.dumps({"ok": False, "error": message, "error_type": "ImportError"}, ensure_ascii=False))
        else:
            print(f"❌ {message}")
        sys.exit(1)

    if cmd == "login":
        if len(args) < 2:
            usage_error("Usage: x-reader login <platform> [--headless] (supported: xhs, wechat, twitter)", code=1)
        headless = "--headless" in args
        cmd_login(args[1], headless=headless)
    elif cmd == "list":
        cmd_list()
    elif cmd == "clear":
        cmd_clear()
    elif cmd.startswith("http") or cmd.startswith("www.") or "." in cmd:
        urls = [
            arg for arg in args
            if arg.startswith(("http", "www.")) or "." in arg
        ]
        if _os.environ.get("X_READER_HOSTED") == "1":
            from x_reader.utils.proc import apply_hosted_limits
            apply_hosted_limits()
        if full_long_audio:
            if len(urls) != 1:
                usage_error("--media-full-long accepts exactly one podcast URL")
            cmd_fetch(urls, json_output=json_output, full_long_audio=True)
        elif full_short_audio:
            if len(urls) != 1:
                usage_error("--media-full-short accepts exactly one podcast URL")
            cmd_fetch(urls, json_output=json_output, full_short_audio=True)
        elif media_preview_seconds:
            cmd_fetch(urls, json_output=json_output, media_preview_seconds=media_preview_seconds)
        else:
            cmd_fetch(urls, json_output=json_output)
    else:
        usage_error(f"Unknown command: {cmd}. Run 'x-reader' with no args for help")


if __name__ == "__main__":
    main()
