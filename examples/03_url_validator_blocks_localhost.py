#!/usr/bin/env python3
# Scene: validate_url 拒绝 localhost，避免 SSRF；本例不发起 DNS 或 HTTP。
# 用于安全相关单测的手动复现脚本。
# 用法: python3 examples/03_url_validator_blocks_localhost.py
# 依赖: x_reader.utils.url_validator（含可选 idna）。
# 期望: 打印 caught 且退出码 0。

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from x_reader.utils.url_validator import validate_url  # noqa: E402


def main() -> None:
    try:
        validate_url("https://localhost/foo")
    except ValueError as e:
        print("caught:", e)
        return
    raise SystemExit("expected ValueError")


if __name__ == "__main__":
    main()
