#!/usr/bin/env python3
# Scenario: Catch `ValueError` from `validate_url` for bad schemes / SSRF patterns.
# Learning goal: surface user-facing errors before spawning fetchers.
# Prereqs: validator module only.
# Edge case: operator confuses `file://` — blocked early.
# Run: python examples/cookbook/07_validate_url_error_handling.py

from __future__ import annotations

from x_reader.utils.url_validator import validate_url


def safe_validate(u: str) -> str | None:
    try:
        return validate_url(u)
    except ValueError as e:
        print("blocked:", e)
        return None


def main() -> None:
    assert safe_validate("http://127.0.0.1/secret") is None
    print("done")


if __name__ == "__main__":
    main()
