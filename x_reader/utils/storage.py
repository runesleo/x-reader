# -*- coding: utf-8 -*-
"""
Storage utilities — save content to JSON inbox and optional Markdown file.

Implements the "atomic archiving" from the tweet:
- unified_inbox.json (for AI/programmatic use)
- markdown file (for human reading, e.g. Obsidian)
"""

import json
import os
from datetime import datetime
from pathlib import Path
from loguru import logger

from x_reader.schema import UnifiedContent


def save_to_json(item: UnifiedContent, filepath: str = "unified_inbox.json"):
    """Append content to JSON inbox file."""
    path = Path(filepath)
    data = []

    if path.exists():
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError):
            data = []

    data.append(item.to_dict())

    # Keep last 500 entries to prevent unbounded growth
    data = data[-500:]

    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    logger.info(f"Saved to JSON: {path}")


def save_to_markdown(item: UnifiedContent, filepath: str = None):
    """
    Append content to a Markdown file (e.g. Obsidian vault).

    Supports two output modes:
    - OUTPUT_DIR: Write to {OUTPUT_DIR}/content_hub.md
    - OBSIDIAN_VAULT: Write to {OBSIDIAN_VAULT}/01-收集箱/x-reader-inbox.md

    If neither is set, skips markdown output.
    """
    if not filepath:
        # Priority 1: Obsidian vault
        vault_path = os.getenv("OBSIDIAN_VAULT", "")
        if vault_path:
            filepath = os.path.join(vault_path, "01-收集箱", "x-reader-inbox.md")
        else:
            # Priority 2: generic output dir
            output_dir = os.getenv("OUTPUT_DIR", "")
            if not output_dir:
                return
            filepath = os.path.join(output_dir, "content_hub.md")

    # Security: Validate filepath to prevent path traversal attacks
    # Only allow paths under explicitly configured directories or current working directory
    abs_filepath = os.path.abspath(filepath)
    
    # Get allowed directories from environment, with fallbacks
    output_dir = os.getenv("OUTPUT_DIR", "")
    vault_path = os.getenv("OBSIDIAN_VAULT", "")
    
    # Build list of allowed absolute paths (skip empty/missing env vars)
    allowed_dirs = []
    if output_dir:
        allowed_dirs.append(os.path.abspath(output_dir))
    if vault_path:
        allowed_dirs.append(os.path.abspath(vault_path))
    # Always allow current working directory
    allowed_dirs.append(os.path.abspath(os.getcwd()))
    # Always allow user's home directory
    allowed_dirs.append(os.path.abspath(os.path.expanduser("~")))
    # Always allow /tmp for temporary files
    allowed_dirs.append("/tmp")
    
    # Check if filepath is in any allowed directory
    if not any(abs_filepath.startswith(d) for d in allowed_dirs):
        raise ValueError(f"Security: Refusing to write outside allowed directories: {filepath}")

    path = Path(filepath)
    path.parent.mkdir(parents=True, exist_ok=True)

    emoji = {
        "telegram": "📢", "rss": "📰", "bilibili": "🎬",
        "xhs": "📕", "twitter": "🐦", "wechat": "💬",
        "youtube": "▶️", "manual": "✏️",
    }.get(item.source_type.value, "📄")

    with open(path, 'a', encoding='utf-8') as f:
        f.write(f"\n## {emoji} {item.title}\n")
        f.write(f"- Source: {item.source_name} ({item.source_type.value})\n")
        f.write(f"- URL: {item.url}\n")
        f.write(f"- Fetched: {item.fetched_at[:16]}\n\n")
        f.write(f"{item.content[:2000]}\n")
        f.write("\n---\n")

    logger.info(f"Saved to Markdown: {path}")


def save_content(item: UnifiedContent, json_path: str = None, md_path: str = None):
    """Save content to both JSON and Markdown."""
    inbox_file = json_path or os.getenv("INBOX_FILE", "unified_inbox.json")
    save_to_json(item, inbox_file)
    save_to_markdown(item, md_path)
