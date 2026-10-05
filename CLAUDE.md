# ClipAgent — read this first

ClipAgent ("ClipAgent Studio") is gs's own clipping tool. It turns long videos (YouTube, Twitch, Kick, TikTok links or
uploaded files) into ranked, styled, checked vertical clips for TikTok, YouTube Shorts and Instagram Reels — and runs
the money side of clipping campaigns (Whop Content Rewards / Reach campaigns): the brief's rules, posting plans, view
tracking, earnings, Telegram updates.

The owner (gs) is **not a programmer**. He runs ClipAgent on his Windows PC and talks to it through the web page and
Telegram. Everything you build must be usable without reading code: plain words on screen, no jargon, every error
explained with what to do next.

Next work is described in:
- `docs/STATUS.md` — every feature that exists today, known issues, and the backlog
- `docs/EDIT_MAKER_PLAN.md` — the Edit Maker (music-driven edits): full spec and build steps
- `docs/CREATOR_SCAN_PLAN.md` — Creator Scan (go through a creator's whole catalog): full spec and build steps
- `docs/SMART_STITCH_PLAN.md` — Smart Stitch (teaser openings, proof/reaction inserts, and clips that combine parts
  from different videos): full spec and build steps
- `docs/VIRAL_EDITS_RESEARCH.md` — the research the Edit Maker is based on

---

## Rules (non-negotiable)

1. **Secrets.** API keys and the Telegram bot token live in `.env`. Never print them, paste them into chat, write them
   into logs, tests, docs or commits. `.env.example` holds placeholders only.
2. **No sign-ups or log-ins on gs's behalf**: don't create accounts, join campaigns, join Discords, or log into
   Whop/TikTok/Instagram/YouTube for him.
3. **Ask before downloading software.** New pip packages, models, fonts or tools need gs's OK first (say what, from
   where, how big). Prefer what's already installed: numpy, OpenCV (cv2), Pillow, ffmpeg, yt-dlp, anthropic, FastAPI.
   (scipy is NOT installed on the PC.)
4. **Windows Defender.** Never try to get around it. If a command is blocked, split it into simpler commands or run
   it in a visible window (a `.bat` started normally). No hidden-window tricks.
5. **Don't delete gs's data** (videos, clips, the database, campaigns). Only delete scratch files you created. Before
   replacing working files with big changes, copy them to `_backup_before_<feature>/` in the project root.
6. **Never fake an ability.** If ClipAgent can't do something, it says so in plain words and offers the closest
   thing it can do.
7. **Campaign rules always win.** A campaign's brief (stored rulebook) decides what's allowed: music, logos, cuts,
   hashtags, lengths, platforms. Never break it to make something look better.
8. When you mention the running app to gs, give the clickable link: [localhost:8000](http://localhost:8000).

---

## Running it

- Project folder on the PC: `C:\Users\gurem\Downloads\clipagent-studio\clipagent-studio` (Python venv in `.venv`).
- Start: double-click **ClipAgent** on the desktop (runs `Open ClipAgent.bat`: starts the server if it isn't running,
  then opens the browser), or run `C:\Users\gurem\Downloads\START_CLIPAGENT.bat` (server only, visible window).
- The server is `python -m uvicorn app.main:app --host 127.0.0.1 --port 8000` → http://localhost:8000
- Restart after changing Python files: stop the uvicorn process (PowerShell:
  `Get-CimInstance Win32_Process | ? { $_.CommandLine -match 'uvicorn' } | % { Stop-Process -Id $_.ProcessId -Force }`)
  then start it again. **Check nothing is rendering first** (`GET /api/videos?limit=5` — no `running`/`queued`),
  or you kill a run in progress. Static files (`static/`, `templates/`) don't need a restart — just reload the page.
- Keys (`.env`): `ANTHROPIC_API_KEY`, `CLAUDE_MODEL` (claude-sonnet-5), Whisper via Groq (`WHISPER_BASE_URL`,
  `WHISPER_API_KEY`, `WHISPER_MODEL`), `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, optional `YTDLP_COOKIES`,
  `RENDER_PROCESSES`, `RENDER_WORKERS`, `CLIPAGENT_ENGINE` (seamless|classic), `DATA_DIR`, `MAX_CLIPS`.
- Machine: Windows 11, Intel i7-1255U (10 cores, no GPU worth using), ffmpeg 9 full build on PATH. Rendering is
  CPU-bound: one clip ≈ 30–90 s.

## Tests

Run from the project root with the venv: `.venv\Scripts\python.exe tests\<name>.py`. Each prints `ok`/`FAIL` lines
and ends with `all checks behaved`. They use a temp `DATA_DIR`, stand-ins for Claude, and never touch real data.

| Test | Covers |
|---|---|
| `ui_api.py` | the endpoints the interface uses: videos list, friendly errors, retry, undo, platform length caps, campaign batches |
| `ask_changes.py` | "Tell ClipAgent what to change" (typed requests), undo, Telegram `/change` |
| `money_side.py` | posts, earnings, planner, reminders, scout, morning summary |
| `telegram_updates.py` | Telegram messages and commands |
| `style_brain.py` | the style brain (looks/recipes, cards, word-pop captions) |
| `clip_doctor.py <clip.mp4>` | the clip doctor (needs any rendered clip as argument) |
| `source_quality.py` | download/source handling |
| `beats_detect.py` | beat + drop detection for the Edit Maker |
| `parallel_render.py <source.mp4> ...` | parallel rendering (slow, needs a real source) |
| `campaign_api.py`, `campaign_gate.py`, `brand_logo.py`, `watch_clips.py` | campaign mode (some need sample files) |

Always run the fast suites (`ui_api`, `ask_changes`, `money_side`, `telegram_updates`, `style_brain`,
`beats_detect`) after a change, and check real output by eye: pull frames with ffmpeg
(`ffmpeg -ss 5 -i data\clips\<id>.mp4 -frames:v 1 frame.jpg`) and look at them.

---

## How it's built

FastAPI backend (`app/`), one-page front end (`templates/index.html`, `static/app.js`, `static/styles.css`), SQLite
(`data/clipagent.db`), files under `data/`. ~13k lines of Python.

### The clip pipeline (`pipeline.run_job`)
1. **Get the video** — `media.py` (yt-dlp download with friendly errors, cookies support, uploads). Sources are kept in
   `data/sources/<job>/source.mp4` so re-runs and edits never download again.
2. **Words** — `transcribe.py` (Whisper with word timestamps, chunked; transcript cached by source fingerprint).
3. **Find moments** — `highlights.py` (Claude reads the transcript in blocks, ranks moments; length window from the
   chosen platforms: Shorts-only ≤ ~52 s, all three 25–35 s ideal, TikTok-only up to 90 s; a campaign's own min/max wins).
4. **Build each moment two ways** — `structure.py` (continuous vs stitched setup+payoff), `judge.py` (picks the better).
5. **Framing** — `framing.py` (face detection with YuNet in `models/`, speaker tracking, facecam detection, two-shot share).
6. **Style brain** — `styles.py` + `cards.py` (looks: word-pop, headline label, title bar, comment bubble, stacked split,
   classic captions; data from `data/research/clip_style_db.json`; can be forced to one look by the user).
7. **Render** — `render.py` → `motion.py` (the "seamless" frame-by-frame engine: smooth speaker tracking, punch-ins,
   impact zooms, cards, captions via `captions.py` ASS + libass with Anton/Poppins from `fonts/`), parallel processes.
   `tighten.py` cuts dead air and fillers.
8. **Clip doctor** — `doctor.py` measures every clip (length for the platform, loudness, black/frozen frames, first-frame
   text, sentence edges), Claude looks at 7 frames, fixes what it can with one re-render; `doctor.recheck` re-measures
   after manual edits.
9. **Campaign gate** — `campaign.py` (reads a brief into a rulebook with Claude; permissions, hashtags, pay terms),
   `compliance.py` (checks every campaign clip; blocked clips can't be downloaded), `overlay.py` (clip-bank campaigns:
   the campaign's own clips posted whole with a hook on top), `brandlogo.py`.
10. **Telegram** — `notify.py` (clips sent when ready, problems explained, commands — see `main.telegram_command`).

### Other parts
- `instruct.py` — "Tell ClipAgent what to change": a typed request → Claude maps it to editor controls per clip →
  re-render in turn, undo snapshot kept (`data/undo/<clip>/`).
- `money.py` — posts, view checks (yt-dlp; Instagram typed by hand), earnings by the campaign's pay terms, posting
  planner (12:00 / 16:30 / 20:30), reminders, channel watch/scout, 9:00 morning summary; background `money.tick` every 60 s.
- `beats.py` — **new, done, tested**: song tempo, beats, bars, the drop (numpy only).
- `edits.py` — **new, draft**: Edit Maker planning (styles, moment picking, beat timeline). Needs `editrender.py`,
  endpoints and UI — see `docs/EDIT_MAKER_PLAN.md`.
- `store.py` — all tables: jobs, clips, transcripts, presets, campaigns, requests, sounds, edits (+ migrations dict
  for added columns).
- `toolio.py` — read Claude's tool calls defensively (Claude sometimes returns JSON-in-a-string).

### API (main.py)
Jobs: `POST /api/jobs` (form: url|file + settings incl. `style_recipe`, `platforms`, `campaign_id`), `POST /api/batch`
(json: urls, settings, optional campaign_id), `GET /api/videos`, `GET /api/jobs/{id}`, `POST /api/jobs/{id}/rerun`,
`GET /api/jobs/{id}/export.csv`, `GET /api/jobs/{id}/download.zip`, `POST /api/jobs/{id}/plan`.
Clips: `GET /api/clips/{id}`, `POST /api/clips/{id}/render` (editor), `POST /api/clips/{id}/undo`,
`POST /api/clips/{id}/text`, `GET /api/clips/{id}/waveform`, `GET /api/clips/{id}/download`.
Asks: `POST /api/jobs/{id}/ask`, `GET /api/jobs/{id}/asks`, `GET /api/asks/{id}`.
Campaigns: `POST /api/campaigns/read|preview`, `POST|GET /api/campaigns`, `GET|PUT|DELETE /api/campaigns/{id}`,
logo upload, `POST /api/campaigns/{id}/jobs` (clip-bank runs).
Money: `GET /api/money`, `POST /api/posts`, `POST /api/posts/{id}`, `/check`, `DELETE`, `POST /api/money/settings`,
`POST /api/watch|unwatch`. Also presets, brand logo, `/media/*`, `/healthz`.

### Front end
- Hash routes: `#/make`, `#/videos`, `#/video/<id>`, `#/campaigns`, `#/campaigns/new`, `#/campaigns/review`,
  `#/campaign/<id>`, `#/campaign/<id>/rules`, `#/money`, `#/settings`.
- Design (Crayo-inspired): sidebar (bottom tab bar on phones), light + dark themes with a switch (tokens on `:root`,
  `[data-theme="dark"]` overrides; accent #5B3DF5 light / #8A72FF dark; pop yellow #FFD400), Plus Jakarta Sans UI font,
  Anton/Poppins served from `static/fonts`. Every page must work at 390 px wide with no sideways scroll.
- The video page has: progress steps, a strip showing where each clip comes from, clip cards (Download / Edit / Copy
  caption / Undo), the "Tell ClipAgent what to change" box. The editor is full-screen: icon rail (Ask, Text, Captions,
  Framing, Post, Check), preview, trim timeline; phone Back closes it.

### Conventions
- Words on screen: specific, never vague teasers; only facts from the video's own words.
- Safe zones for 1080×1920: keep text out of the top 190 px, bottom 400 px, right 140 px.
- Errors shown to gs go through `main.friendly_error` (raw detail kept behind "Technical details").
- Long work runs in background threads; the page polls. Anything slow reports progress in plain words.
- A failed re-render never destroys the working clip.

## Current campaigns (in the app)
TJR — Reach ($0.75 per 1K views, pays after 10K views, $7.50 min, $750 max per post, #TJR required, no logos, no
AI-generated video, TikTok/Reels/Shorts, never portray TJR negatively). Sources: youtube.com/@TJRTrades,
youtube.com/@TRichesTrades, kick.com/tjr, instagram.com/tjr. Others: Kevin Langue, Eat Everything, Lovable, Gamebred
(clip-bank), Adriatique.
