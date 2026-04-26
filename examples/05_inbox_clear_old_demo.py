#!/usr/bin/env python3
# Scene: UnifiedInbox.clear_old 按 fetched_at 剔除过期项；用伪造时间戳演示行为。
# 使用临时 JSON 文件，跑完删除。
# 用法: python3 examples/05_inbox_clear_old_demo.py
# 依赖: x_reader.schema。
# 输出: clear 前后条数。

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from x_reader.schema import UnifiedInbox  # noqa: E402


def main() -> None:
    old = {"source_type": "manual", "source_name": "x", "title": "old", "content": "c",
           "url": "https://example.org/old", "fetched_at": "2000-01-01T00:00:00"}
    new = {"source_type": "manual", "source_name": "x", "title": "new", "content": "c",
           "url": "https://example.org/new", "fetched_at": "2099-01-01T00:00:00"}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
        path = f.name
        json.dump([old, new], f)
    try:
        inbox = UnifiedInbox(path)
        print("before", len(inbox.items))
        inbox.clear_old(days=7)
        print("after", len(inbox.items), "titles:", [i.title for i in inbox.items])
    finally:
        Path(path).unlink(missing_ok=True)


if __name__ == "__main__":
    main()
