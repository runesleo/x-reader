#!/usr/bin/env python3
# Scene: UnifiedInbox 在同一路径上对相同 URL 的条目去重（第二次 add 返回 False）。
# 使用临时文件，不污染仓库内 inbox。
# 用法: 仓库根目录 python3 examples/02_inbox_dedup_tempfile.py
# 依赖: x_reader 包可导入。
# 退出码: 断言失败则非 0。

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from x_reader.schema import UnifiedContent, UnifiedInbox, SourceType  # noqa: E402


def main() -> None:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        path = f.name
    try:
        inbox = UnifiedInbox(path)
        a = UnifiedContent(
            source_type=SourceType.MANUAL,
            source_name="demo",
            title="t",
            content="c",
            url="https://example.org/unique-dedup-demo",
        )
        assert inbox.add(a) is True
        assert inbox.add(a) is False
        print("dedup ok, count=", len(inbox.items))
    finally:
        Path(path).unlink(missing_ok=True)


if __name__ == "__main__":
    main()
