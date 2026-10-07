# Creator Scan — go through a creator's whole catalog

**Goal.** Instead of clipping one video at a time, gs points ClipAgent at a *creator* (e.g. TJR: two YouTube channels,
a Kick channel, an Instagram) and ClipAgent goes through **everything they've posted**, finds their best moments of each
kind — crazy/hype, funny, success/wins, money, quotes, reactions — ranks them across the whole catalog, and turns them
into clips, compilations and edits. It takes hours; that's fine. It must be resumable, polite to the platforms, cheap,
and show progress in plain words.

**Is it possible? Yes.** The trick is to *read* everything cheaply and *download* only what's worth it:
1. List every video (fast, metadata only).
2. Read every video's words from the platform's own subtitles (free, no video download). Only when there are no
   subtitles, download the audio alone and transcribe it.
3. Find and score moments from the words + extra signals ("most replayed" heat, loudness/laughter, view outliers).
4. Download only the winning *sections* (a few seconds to a minute each), not whole videos.
5. Check the picture (motion, faces, a Claude look at frames) for the "crazy movement" kind of moment.
6. Make clips / compilations / edits from the best moments across the catalog.

Read `CLAUDE.md` first (rules: no sign-ups/log-ins for gs, ask before new packages, don't hammer YouTube).

---

## 1. Data model (add to `store.py`)

- `creators`: id, name, links (JSON list), campaign_id (optional), created_at, last_scan_at, settings (JSON: kinds to
  look for, date range, min views, include shorts/streams).
- `catalog`: id, creator_id, platform, video_id, url, title, duration, upload_date, views, likes, kind
  (video/short/stream/vod/clip), status (`listed → words → scored → sourced → done | skipped | failed`), words_source
  (subs/whisper/none), heatmap (JSON), error, updated_at. Unique (platform, video_id).
- `catalog_words`: catalog_id, transcript JSON (segments + words when known) — reuse `transcribe` formats.
- `moments`: id, creator_id, catalog_id, start, end, hit, kind (crazy/funny/success/money/quote/reaction/hype/story),
  score (0–100), signals (JSON: text score, heat, loudness, laughter, motion, views factor), text (what's said),
  hook (suggested), status (`candidate → fetched → checked → used`), section_path (downloaded section), used_in (JSON:
  clip/edit ids).
- `scan_jobs`: id, creator_id, stage, progress, counters (listed/read/scored/fetched), started_at, finished_at, error.

## 2. Listing the catalog (`app/catalog.py`)
- YouTube: `yt-dlp --flat-playlist -J <channel>/videos`, `/shorts`, `/streams` (each tab separately). Fields: id,
  title, duration, view_count, upload_date (flat lists may lack dates — fetch per video later, lazily).
- Kick: try yt-dlp on the channel's videos page; if it can't list, fall back to Kick's public channel API only if it
  works without logging in; otherwise ask gs to paste VOD links (never log in for him). Clips: kick.com/<name>/clips.
- Instagram: yt-dlp needs cookies for most profiles → optional; skip with a clear note if it fails. Never log in for gs;
  use `YTDLP_COOKIES` only if gs set it up himself.
- TikTok profile: optional, same rule.
- Filters gs can set: date range (e.g. last 12 months), skip shorts (they're already clips), min duration, only videos
  with ≥ X views. Default: everything long-form + streams, newest first.
- De-duplicate across channels (same title + similar duration).
- Store `view outlier` = views ÷ channel median (videos that outperformed get scanned first).

## 3. Reading the words (cheap first)
- YouTube: `yt-dlp --skip-download --write-subs --write-auto-subs --sub-langs "en.*" --sub-format vtt` → parse VTT
  (auto-captions repeat lines; de-duplicate rolling text) into segments with times. No Whisper cost, no video download.
- No subtitles (Kick VODs, some videos): download **audio only** (`-f bestaudio`), transcribe with `transcribe.py`.
  Groq's free tier limits audio per hour/day — read the limit errors, queue, and pause/resume; show "Waiting for
  transcription quota" instead of failing. Long VODs: chunk (existing `AUDIO_CHUNK_SECONDS`).
- Throttle: max ~1 request every few seconds to YouTube, back off on 429 / bot-check, stop the scan with a plain
  message if the PC gets bot-blocked (see `media.explain_download_error`). Never use proxies or tricks to get round it.
- Cache everything: re-scans only read new uploads.

## 4. Finding and scoring moments
Per video, cheapest model first:
1. **Screen** each transcript with Claude (a cheaper/faster model is fine, e.g. a Haiku-class model set by
   `CLAUDE_SCREEN_MODEL`; default to `CLAUDE_MODEL` if unset) in blocks: return candidate moments with kind, start,
   end, hit, a 0–10 score and a one-line reason. Kinds: **crazy/hype** (wild statements, shouting, big reactions),
   **funny** (jokes, banter, chaos), **success/money** (wins, P&L reveals, big numbers, lifestyle flexes),
   **quote** (memorable lines, advice), **reaction**, **story**.
2. **Signals** added per candidate:
   - YouTube "Most replayed" heatmap (yt-dlp `heatmap` field: list of {start_time, end_time, value}) → peak value inside
     the moment (very strong signal of what viewers rewatch).
   - Loudness / laughter / shouting from the audio (when audio was downloaded; otherwise later on the section).
   - Video outlier factor and recency.
   - Chat spikes for streams when a chat replay is available (old ClipAgent idea; optional).
3. **Rank across the catalog** with Claude (Sonnet-class `CLAUDE_MODEL`) on the top ~100 candidates per kind: one
   scale, de-duplicate the same story told in several videos (keep the best telling), write a hook for each.
4. Respect campaign guidance (TJR: never negative; must feature TJR).

## 5. Fetching only what's needed
- For the top N moments (gs chooses N, default 40): `yt-dlp --download-sections "*<start>-<end>" --force-keyframes-at-cuts`
  with ±8 s padding → `data/sections/<creator>/<video>_<start>.mp4`. Keep the source link and offsets so the pipeline
  can trim precisely and re-fetch if needed.
- Rate-limit downloads (e.g. one at a time, a pause between), resume after failures.

## 6. Checking the picture ("crazy movements")
For each fetched section:
- Motion intensity: frame differencing / optical flow (OpenCV DIS) → peaks of movement; faces (YuNet in `models/`) →
  is the creator on screen and big enough; scene cuts.
- A Claude look at 4–6 frames for kinds where the picture matters (crazy, reaction, funny): "Is this visually a strong
  moment? Is the creator clearly visible? 1–10." Update the score; drop moments where the creator isn't visible
  (campaign rule "must feature TJR").

## 7. What gs can do with the results
- **Moments browser** (new page "Creators" → creator → moments): filters by kind, score, video, date; play each moment
  (from the section); select many.
- **Make clips** from selected moments → each becomes a job using the section as the source (existing pipeline:
  framing, style brain, doctor, campaign gate). Clip titles/hook from the moment.
- **Compilation**: "Top 5 funniest TJR moments" — moments back to back with numbered cards (new small renderer or reuse
  the Edit Maker with a "compilation" style).
- **Edit**: send selected moments to the Edit Maker (`docs/EDIT_MAKER_PLAN.md` §3: `pick_moments` accepts pre-scored
  moments) — "Velocity edit of his 10 biggest wins".
- **Search**: "every time he talks about his first million" → full-text over `catalog_words` + Claude to pick the
  best → results as moments.
- **Keep watching**: the existing channel watch (`money.scout`) adds new uploads to the creator's catalog and scans
  them automatically; Telegram: "3 new moments from today's TJR stream — want clips?"

## 8. Interface
Sidebar: **Creators**. Add a creator: name + links (pre-filled from a campaign's source links — TJR's brief lists
youtube.com/@TJRTrades, youtube.com/@TRichesTrades, kick.com/tjr, instagram.com/tjr). Settings (kinds, date range, how
many moments to fetch). **Start scan** → a progress panel in plain words: "Found 412 videos · Read 160 of 412 ·
38 great moments so far · Waiting 20 min for transcription quota". Pause / resume. Telegram updates at milestones.

## 9. Cost and time (tell gs before a big scan)
- Show an estimate before starting: number of videos and hours of speech; how many need Whisper vs have subtitles;
  rough Claude cost (screening ≈ the transcript's length in tokens; e.g. 300 hours of speech ≈ a few million input
  tokens); download size for sections only; expected time.
- Defaults that keep it cheap: subtitles first, screening model, only top-N sections downloaded.

## 10. API (sketch)
`POST /api/creators` · `GET /api/creators` · `GET /api/creators/{id}` · `POST /api/creators/{id}/scan` (start/resume)
· `POST /api/creators/{id}/pause` · `GET /api/creators/{id}/moments?kind=&min=&q=` · `POST /api/moments/clips`
(selected → jobs) · `POST /api/moments/edit` (selected → Edit Maker) · `POST /api/creators/{id}/search` (text query).

## 11. Tests
- VTT parsing (auto-caption rolling duplicates) → clean segments with times.
- Catalog listing parse from a saved `yt-dlp -J` sample (no network in tests).
- Scoring merge (text score + heatmap + outlier) → stable ranking; de-duplication of the same story.
- Section fetch command building (`--download-sections`), resume logic, quota-wait handling (Whisper 429 stub).
- Moment → job: a section becomes a normal run with the right offsets.

## 12. Done means
- Pointing ClipAgent at TJR lists his whole catalog, reads it (mostly via subtitles), and within a few hours shows a
  ranked moments browser with real, playable funny / crazy / success moments from many videos; gs selects 10 and gets
  10 checked campaign clips (and/or an edit) without downloading whole videos.
- A scan can be paused, survives a restart, and never gets the PC blocked by hammering YouTube.
