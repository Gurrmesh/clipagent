# ClipAgent — what exists, what's broken, what's next

Last updated: 5 Oct 2026 — the Edit Maker is built (waiting for gs to try it on the PC).

## 1. Features that exist and work (tested on the PC)

### Make clips
- Paste one link, several links (one per line → queued batch), or drop a file. YouTube, Twitch, Kick, TikTok, any
  yt-dlp site. Friendly download errors (bot check, private, members-only, age, unsupported link…).
- "Where will you post?" chips (TikTok / YouTube Shorts / Instagram Reels) set the clip length window.
- Look gallery: "Let ClipAgent choose" (style brain), Word-pop, Headline label, Title bar, Comment bubble, Stacked
  split, and six classic caption styles (Impact, Karaoke, Clean, Boxed, Neon, Streamer).
- How many clips (stepper), More options (framing, caption position, clip doctor, cut dead air, drop fillers,
  follow the speaker, camera moves, two versions, small headline, logo watermark), saved presets.
- Two versions of each moment (continuous / stitched) judged against each other.
- Clip doctor checks and fixes each clip; report on the card and in the editor.

### My videos
- Library of every run (filters: working, done, didn't work), readable errors, "Try again" (re-downloads a failed
  link, or re-runs from the copy on disk), "Run again".
- Video page: progress steps ("Failed at: <step>" when it fails), strip of where clips come from, filters
  (Ready to post / Check first), Download all (zip), Spreadsheet (csv), Plan posts.
- **Tell ClipAgent what to change**: typed requests for one, several or all clips → re-made one after another, with
  Undo. Also in the editor's Ask tab and Telegram `/change`.
- Editor: Ask, Text (hook/card/headline), Captions (look, position, size, words), Framing (layout, crop, facecam,
  camera moves, cut dead air), Post (caption + hashtags, saved), Check (campaign check + clip doctor). Trim timeline
  with waveform and click-a-word. Re-render keeps an undo copy; a failed re-render keeps the old clip.

### Edits (the Edit Maker) — built in Claude Code, to be tried on the PC
- **Edits** in the sidebar. Make an edit: tick one or more finished videos, pick one of 7 styles (Velocity, Aura,
  Flow, Cinematic, Motivation, Funny, Money — each card plays a tiny sample), pick a song (yours: MP3/M4A/WAV or a
  video's sound; play, BPM, drop marked on its loudness curve) or No music for the voice styles, say what it's
  about, the length, and optionally a campaign.
- Claude picks the moments and writes the hook (numbers on screen must be ones he said). Cuts land exactly on the
  beat, the best moment's hit on the drop, the edit ends on a bar line and dissolves back into its first frame so it
  loops. Speed ramps, slow-mo, flashes, zoom punches, shake, RGB glitch, motion-blurred cuts (Flow chains shots so
  the movement carries on), push-ins, grain, dark corners, cinema bars, one colour grade over every video (each
  moment's colour matched first). Voice styles keep every sentence whole and duck the song under his voice. Sound
  at -14 LUFS.
- The edit page: looping preview, Download, Copy caption, Undo; "Tell ClipAgent what to change" ("faster", "black
  and white", "put the 2 million line on the drop", "different song"); change it by hand (style, song, the part of
  the song, colour, length, cut speed, flashes, every effect, voice/music, hook, moments: words, on/off, on the drop,
  order); Re-make (no Claude) or New moments; versions with your other songs; the caption to post.
- Campaigns: refused in plain words when the brief doesn't allow joining moments, cropping or music; speed changes,
  zooms, text, captions or bars it doesn't allow are switched off with a note; the brief's lines and hashtags go in
  the caption; the finished edit goes through the campaign check (blocked = no download without "anyway").
- Telegram: finished edits are sent with the caption and verdict; `/edit velocity his biggest wins` makes an edit of
  the last video with the last song.
- A 20 s edit renders in ~25 s on a 4-core cloud machine (the PC should be similar).

### Campaigns
- Paste a brief → Claude reads it into a rulebook (permissions with quotes and line numbers, grey areas to decide,
  hashtags, caption lines, lengths, brand logo rule, pay terms, platforms, posting rules) → review card → save.
- Two kinds: clip-from-footage (rules applied to normal runs) and clip-bank (their clips posted whole with a hook).
- Every campaign clip goes through the gate (Ready / Check first / Blocked); blocked clips can't be downloaded
  (with a deliberate "Download anyway"). Post kit with the exact caption to paste and the posting checklist.
- Campaign page: stats (videos made, posts, views, earned), make clips for it (one or many links).

### Money
- Track posts by link; views checked automatically for TikTok/YouTube (Instagram typed in). Milestone pings.
- Earnings by each campaign's pay terms (rate per 1K, minimum views, max per post; a rate added later counts).
- Posting planner across your accounts (2–3 a day, 3 h apart) with Telegram reminders carrying the clip.
- Channel watch (Telegram ping on new uploads), 9:00 morning summary, views-per-day chart, which looks work for you
  (fed back into the style brain).

### Telegram (@Gs_clipagent_bot)
Send a link to clip it (`link 6`, `link lovable`, `link shorts`), `/status`, `/clips`, `/retry`, `/plan`,
`/posted N link`, `/views N 34k`, `/money`, `/summary`, `/accounts tiktok @you`, `/watch link`, `/unwatch link`,
`/campaigns`, `/change 2 end it after the punchline`, `/help`.

### Settings & look
Light / dark / match computer; Telegram status and setup steps; brand logo + corner + caption highlight colour
(saved in the browser); connection status for Claude and transcription. Desktop shortcut "ClipAgent".

## 2. Known issues / watch-outs
- **YouTube bot check**: after many downloads YouTube may block the PC for hours. Fix: `YTDLP_COOKIES` (cookies.txt
  from a spare account), wait, or upload the file. Don't hammer downloads.
- **Instagram views** can't be read automatically (typed in by hand).
- **Rendering is CPU-bound** (i7-1255U): ~30–90 s per clip; the process pool gives ~12%.
- **Clip length vs platform**: the length ceiling by platform (Shorts ≤ ~52 s) was added on 5 Oct; confirm on new runs
  that clips stay in range (the clip doctor flags long ones).
- **Brief reading isn't perfect**: e.g. TJR's "no reposts/collab posts" was misread as "no joining moments" — gs's
  review step fixes this; keep the review card prominent.
- A campaign brief that says nothing about music → music counts as NOT allowed until gs allows it in the rules.
- Undo is one step deep. Typed change requests can't create a brand-new clip from another part of the video yet.
- The editor needs the original video on disk (`data/sources`) to trim or re-render.
- Test leftovers in My videos: two "Conan O'Brien…" re-runs made while testing (gs can delete).
- **Edit Maker, still to check on the PC**: typed changes and moment picking were only tested with a stand-in for
  Claude (no key in the cloud); face framing in edits was tested on drawn faces, not real footage.
- **Beat finder on some songs**: on a song whose hi-hats are much louder than its kick drum, the beat finder can lock
  onto the off-beats — every cut would then land between beats. Watch the first edits on new songs; if it happens,
  tell Claude Code which song.

## 3. In progress at hand-over
- **TJR campaign**: saved as "TJR — Reach"; a batch of 3 TJR videos was queued (7 Years of Trading Advice in 10
  Minutes; TJR Reacts to the TJR and Aiden videos; Teaching My Friend How To Day Trade). The first finished (6 clips:
  5 ready, 1 to check). Check the others in My videos.
- **Edit Maker**: built and tested in the cloud (pull request open) — waiting for gs to try it on the PC.

## 4. Backlog, in order
1. ~~Edit Maker — `docs/EDIT_MAKER_PLAN.md`~~ built (see above).
2. Creator Scan (whole-catalog search) — `docs/CREATOR_SCAN_PLAN.md`
3. Smart Stitch — `docs/SMART_STITCH_PLAN.md`: Part 1 (teaser opening, proof/reaction/callback inserts, stitching
   funny/hype moments when a setup elsewhere helps), Part 2 (clips that combine parts from different videos: then vs
   now, prediction → result, same advice said many times…), Part 3 (the same across a creator's whole catalog).
   Today: only the "stitched" version inside one video (`structure.py`), never for funny/hype moments, no teasers,
   no mid-clip inserts, nothing across videos.
4. Typed requests that add a new clip ("make another clip of the part where he gets the call").
5. Multi-step undo / version history per clip.
6. Compilations: "Top 5 funniest moments" as one video with numbered cards (shares Creator Scan + Edit Maker parts).
7. Hook A/B: two hooks per clip, track which wins (money.py has the data).
8. Smarter Instagram views (screenshot reading or the user's own Instagram insights if they connect it).
