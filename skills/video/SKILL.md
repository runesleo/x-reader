---
name: video
description: Transcribe and summarize video or podcast links, including YouTube, Bilibili, X video, Xiaoyuzhou, Apple Podcasts, and direct media URLs.
---

# Video & Podcast Digest

Use this skill when the media itself must be consumed, not merely the surrounding page text.

Follow the detailed platform and transcription workflow in [skill.md](./skill.md).

Critical contract:

- Reading a post caption is not equivalent to reading its attached video.
- Prefer platform subtitles first, then audio transcription.
- If the media cannot be accessed, return a partial/failure state instead of inferring its contents.
