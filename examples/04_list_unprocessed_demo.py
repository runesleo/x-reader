#!/usr/bin/env python3
# Scene: 内存中构造两条 UnifiedContent，一条已处理一条未处理，演示 get_unprocessed。
# 不写磁盘。
# 用法: python3 examples/04_list_unprocessed_demo.py
# 依赖: x_reader.schema。
# 输出: 未处理条目标题。

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from x_reader.schema import UnifiedContent, SourceType  # noqa: E402


def main() -> None:
    items = [
        UnifiedContent(
            source_type=SourceType.RSS,
            source_name="feed",
            title="done",
            content="x",
            url="https://example.org/a",
            processed=True,
        ),
        UnifiedContent(
            source_type=SourceType.RSS,
            source_name="feed",
            title="pending",
            content="y",
            url="https://example.org/b",
            processed=False,
        ),
    ]
    pending = [i for i in items if not i.processed]
    print("unprocessed:", [p.title for p in pending])


if __name__ == "__main__":
    main()
