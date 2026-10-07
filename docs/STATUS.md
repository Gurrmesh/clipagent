# ClipAgent — what exists, what's broken, what's next

Last updated: 6 Oct 2026 — fixes after the first real TJR clips and edits (Part 1); Creator Scan and Smart
Stitch are being built.

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

### Fixes after the first real TJR clips and edits (6 Oct) — built in Claude Code, to be tried on the PC
- **Edit moments the right length**: every style has a moment length window enforced in code (Funny 3–8 s, Velocity
  1.5–4 s, Motivation 4–10 s, Aura 1.5–5 s, Flow 0.75–3 s, Cinematic 4–12 s, Money 1.5–5 s). Each moment is cut to its
  punchline on word boundaries (voice styles on sentence edges); a moment that can't be cut cleanly is left out with
  the reason. The finished edit stays within ±15% of the length asked (and inside a campaign's min/max). "Tell
  ClipAgent what to change" can shorten, lengthen or trim one moment ("cut the first 2 seconds of moment 3", "end the
  first moment right after he says …"); the edit page has Shorter / Longer per moment.
- **Brief reader**: now reads the "What to look for" list (given to the moment pickers), date rules ("clips from 2026
  onwards" — older videos are refused right after the download, before any transcribing), "caption / text overlay
  must mention TJR" (added in code when the writer forgets; the check blocks it if missing), "TJR must be the primary
  focus", "no logos" (anywhere in the video) and "no AI-generated video". "No reposts / collab posts" is no longer read
  as "no joining moments" (it's a posting rule). Saved campaigns: Rules → **Read the brief again** adds the new rules
  and keeps every choice gs made.
- **Downloads**: when YouTube asks "confirm you're not a bot", ClipAgent pauses YouTube downloads (links stay saved,
  other sites keep going), explains what to do (wait, cookies from a spare account, or upload the file), tries once by
  itself after ~45 min and has a **Try again now** button; one Telegram message, `/resume`. The pause survives a
  restart, and queued links run again after a restart instead of failing. Streams longer than 4 hours download only
  their liveliest parts (found by listening to the sound only first), joined into one source; no clip crosses a join;
  clip cards say where in the stream each clip is. Every job keeps the video's facts (upload date, channel…).
- **Who is on screen and talking** (campaign clips): faces are tracked through the clip, the one whose mouth moves
  with the words is the talker, and the creator is recognised from reference faces (photos gs adds on the campaign
  page — "Who is TJR?" — plus faces learned from his solo videos). One Claude look per clip (context only, never face
  recognition) says who says the key lines. If the brief needs the creator as the main person, a clip where someone
  else does the talking is Blocked ("Timmy is the one talking in this clip…"); otherwise a hook/caption that credits
  the creator with someone else's words is rewritten. The moment picker also records who speaks each moment.
- **More campaign checks**: logos, sponsor banners, "use code" promos and watermarks in the footage (Blocked when the
  brief bans logos, with what/where/when); offensive words and slurs (cut out with one re-render when short and not
  the hook or punchline, otherwise Blocked); AI-generated footage on screen (Blocked when the brief bans AI). Spoken
  promos or AI mentions alone → Check first. Edits get the same checks.
- **No half-cut words**: big titles burned into the creator's video are found (OpenCV only) and the vertical crop
  keeps them whole, keeps them fully out, or shows the whole picture; the clip's note says what was done. The hook and
  cards avoid faces and his titles in every layout. Moments where a title forces the whole picture rank a little
  lower. Edit Maker moments follow the same rule.
- **Smaller files**: every final video encode is capped at ~11 Mbps (a 27 s Motivation edit with grain: 207 MB →
  39 MB); grain is brightness-only so it stays grain. Clips and edits still over 50 MB get a **Phone copy** (under
  ~45 MB, same picture size, sound and length) made in the background; the card and the edit page offer it, Telegram
  sends it instead of a too-big file, and it is made again whenever the clip changes.
- **Edit words never over his face**: the edit renderer follows where his face is in every frame (zooms, punches,
  shakes, a frame moved for a title) and puts the Motivation build-up words, punch/quote/meme text, subtitles and the
  hook above his head or below his chin, smaller when needed — never over his eyes or mouth.
- **Small facecam streams/reactions**: split screen with the face big on top (45% of the height) and the game/video
  below, captions at the seam; kept through re-renders, doctor fixes and undo.
- **Tidy-ups**: test tools back in `tools/`, the icon in `static/`, READMEs restored, every test prints safely on
  Windows when its output goes to a file, `-vsync 0` replaced, and tests that used to write into the real `data/`
  folder now always use a temporary folder.

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
- **Brief reading isn't perfect**: it is now much better (see the 6 Oct fixes) but still depends on Claude; the
  backup checks in code only catch plain wordings. Keep the review card prominent.
- **TJR campaign, joining moments**: the brief doesn't say whether joining moments is allowed, so edits and stitched
  clips stay off until gs answers "Join different moments?" on the campaign's Rules page.
- **Who's on screen (6 Oct)**: face matching works without a face-recognition model, so it is approximate; its
  thresholds were set on drawn faces. Add 1–3 clear photos of TJR on the campaign page. Costs one extra Claude look
  per campaign clip (~10k tokens). Without a Claude key every campaign clip shows "Check first".
- **Burned-in text (6 Oct)**: the detector misses ~5–10% of titles whole (an end letter, a short word on a busy
  background); low-contrast text with no outline isn't found. The classic engine has no text check.
- **Long streams (6 Oct)**: Twitch chat replay isn't available through yt-dlp any more, so the liveliest parts are
  found by loudness only (quiet great moments can be missed); downloading exact parts re-encodes them (~20–40 min on
  the PC for 60 min of 1080p60). The Edit Maker could still cut a moment across a join in a long-stream source.
- **Not tried against the real sites yet**: the bot-check pause and the long-stream parts were tested offline only.
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
- **Fixes after real use (Part 1)**: built and tested in the cloud (pull request open) — waiting for gs to try them.

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
