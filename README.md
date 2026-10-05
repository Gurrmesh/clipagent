# ClipAgent Studio

gs's own clipping tool. It turns long videos (YouTube, Twitch, Kick, TikTok links or uploaded files) into ranked,
styled, checked vertical clips and music edits for TikTok, YouTube Shorts and Instagram Reels, and runs the money
side of clipping campaigns (the brief's rules, posting plans, view tracking, earnings, Telegram updates).

## Start it

Double-click **ClipAgent** on the desktop (it runs `Open ClipAgent.bat`), then use the page at
[localhost:8000](http://localhost:8000).

## Where things are

- `CLAUDE.md`: how it's built and the rules. Read this first.
- `docs/STATUS.md`: what works today, known issues and what's next.
- `docs/*_PLAN.md`: the plans for the bigger features.
- `app/`: the server (Python). `templates/` and `static/`: the page. `tests/`: the checks.
  `tools/`: helper scripts. `fonts/`, `models/`: fonts and the face detector.
- `data/`: your videos, clips, edits, songs and the database. It never goes into git.

Keys and the Telegram token live in `.env` (see `.env.example`). Never share that file.
