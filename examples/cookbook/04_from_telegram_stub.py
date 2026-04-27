#!/usr/bin/env python3
# Scenario: Build `UnifiedContent` from a stub Telegram message dict.
# Learning goal: `from_telegram` normalizes channel metadata into schema.
# Prereqs: schema helpers only; no Telethon session.
# Edge case: missing `text` yields empty title; still valid object.
# Run: python examples/cookbook/04_from_telegram_stub.py

from __future__ import annotations

from x_reader.schema import from_telegram


def main() -> None:
    msg = {"text": "hello world", "url": "https://t.me/demo/123", "views": 42}
    u = from_telegram(msg, channel_name="Demo", channel_username="demo")
    print(u.source_type.value, u.extra.get("views"))


if __name__ == "__main__":
    main()
