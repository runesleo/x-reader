#!/usr/bin/env python3
# Scenario: Mirror `read_batch` semantics with stub coroutines (error handling).
# Learning goal: `asyncio.gather(..., return_exceptions=True)` preserves partials.
# Prereqs: Python 3.10+ asyncio.
# Edge case: exceptions stay in results list — filter before downstream merge.
# Run: python examples/cookbook/05_async_batch_gather_errors.py

from __future__ import annotations

import asyncio


async def maybe_fail(i: int) -> int:
    if i == 1:
        raise ValueError("boom")
    return i


async def main() -> None:
    tasks = [maybe_fail(i) for i in range(3)]
    out = await asyncio.gather(*tasks, return_exceptions=True)
    ok = [x for x in out if not isinstance(x, Exception)]
    bad = [x for x in out if isinstance(x, Exception)]
    print("ok", ok, "errors", len(bad))


if __name__ == "__main__":
    asyncio.run(main())
