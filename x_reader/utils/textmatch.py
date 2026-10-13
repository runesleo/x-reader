# -*- coding: utf-8 -*-
"""Compare two texts by shared runs of words, the way a reader would notice a match."""

from __future__ import annotations

import re
import unicodedata
from collections import Counter

SHINGLE = 6
MAX_TOKENS = 50_000
# CJK, kana and hangul count per character; every other script per word.
_TOKEN = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]|[^\W_]+")


def shingles(text: str) -> Counter | None:
    """Runs of six words (or CJK characters) with their counts; None when too long to compare."""
    tokens = _TOKEN.findall(unicodedata.normalize("NFC", text).casefold())
    if len(tokens) > MAX_TOKENS:
        return None  # a cut-off count would overstate coverage
    return Counter(tuple(tokens[i:i + SHINGLE]) for i in range(max(0, len(tokens) - SHINGLE + 1)))


def overlap(first: str, second: str) -> tuple[float, float] | None:
    """Share of each text's word runs found in the other (repeats counted), or None."""
    a, b = shingles(first), shingles(second)
    if not a or not b:
        return None
    shared = sum((a & b).values())
    return shared / sum(a.values()), shared / sum(b.values())
