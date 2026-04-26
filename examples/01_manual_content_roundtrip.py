#!/usr/bin/env python3
# Scene: 演示 UnifiedContent 手工条目：构造 → to_dict → from_dict 往返无丢失。
# 不读写 unified_inbox.json、不访问网络。
# 用法: 在仓库根目录 python3 examples/01_manual_content_roundtrip.py
# 依赖: 已安装包或 PYTHONPATH=.
# 输出: 打印 title 与 id 供肉眼核对。

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from x_reader.schema import UnifiedContent, from_manual  # noqa: E402


def main() -> None:
    item = from_manual("Demo note", "Body text for example.", "https://example.org/demo")
    blob = json.dumps(item.to_dict(), ensure_ascii=False)
    back = UnifiedContent.from_dict(json.loads(blob))
    assert back.title == item.title
    print("id=", back.id, "title=", back.title[:40])


if __name__ == "__main__":
    main()
