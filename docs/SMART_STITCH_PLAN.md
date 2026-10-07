# Smart Stitch — better clips by combining parts (inside one video, and across many videos)

**Goal.** Today every clip comes from one video, and the only combining ClipAgent does is the "stitched" version in
`structure.py` (2–4 parts in story order: premise → before → payoff, with time-jump labels; never for funny/hype
moments). Smart Stitch makes clips more engaging by:

- **Part 1 — inside one video:** a teaser opening, short inserts in the middle of a clip (proof shots, reactions,
  callbacks), and stitching funny/hype moments when a setup or callback elsewhere makes them land harder.
- **Part 2 — across videos:** given several videos (a batch, a campaign's videos, or later a creator's whole catalog),
  ClipAgent judges on its own which parts from *different* videos belong together — then vs now, a prediction and its
  result, the same advice said five times, a running joke — and stitches them into one clip.
- **Part 3 — hook into Creator Scan** once `docs/CREATOR_SCAN_PLAN.md` is built.

Read `CLAUDE.md` first (rules, tests, how to restart safely). Build in this order: Part 1 → Part 2 → Part 3. Plan
first, show gs, wait for his OK, then build step by step with tests after each step.

---

## 0. Hard rules for every stitched clip (honesty + campaign)

These apply to Part 1 and Part 2 and are checked in code, not only asked of Claude:

1. **Never change what someone said.** Each part is a complete thought (starts on the first word of a sentence, ends
   on the last word of one). Never join pieces so a person seems to say something they didn't, or to answer a question
   they weren't answering.
2. **A reaction only follows what it was really reacting to.** Don't put a laugh or a shocked face after something it
   wasn't a reaction to.
3. **Time labels are true.** "2023 → 2026", "3 YEARS LATER" come from real upload dates / real timestamps. If a date is
   unknown, use a neutral label ("LATER", "ANOTHER STREAM") — never guess a number.
4. **Quotes are verified.** Every part Claude proposes must quote its words; code checks the quote against the
   transcript at those times (fuzzy match ≥ 0.8 after normalising case/punctuation). No match → the part is dropped.
5. **Campaign rules win.** Every stitched clip still goes through the clip doctor and the campaign gate
   (`compliance.py`). TJR: never portray him negatively — a "blew my account → now" arc is fine because it ends on the
   win; a stitch that only shows a loss is not. If a campaign forbids added sound, no whoosh/rewind sounds.
6. **Platform length caps still apply** (Shorts ≤ ~52 s etc., from `highlights.py` / the campaign's min/max).

---

## Part 1 — inside one video

### 1A. Teaser opening ("flash-forward")
Many top clips open on 1.5–3 s of the best part, then go back and tell the story, so the viewer knows a payoff is
coming.

- `structure.py`: for every clip version, Claude may propose `teaser: {start, end, why}` — the single strongest
  1.5–3 s inside the payoff (the reaction, the number on screen, the punchline's first words). Code checks it lies inside
  the payoff part and is 1.2–3.5 s.
- Built as an extra first part with `role: "teaser"`. After it, a short marker: a label ("HOW IT STARTED", "BUT FIRST",
  or empty) plus a quick visual rewind (0.3–0.5 s: fast reverse of the last teaser frames with a slight blur) — visual
  only; a "rewind" sound effect only when the campaign allows added audio.
- The payoff still plays in full later (the teaser's words repeat; `pipeline.build_parts` currently drops overlap only
  for neighbouring parts — keep the teaser exempt from the "never repeat a word" rule).
- Captions for the teaser are shown like the rest.
- **Which clips get one:** setting "Open with a teaser of the best part": *Let ClipAgent decide* (default) / *Always* /
  *Never*. "Decide" = the judge (`judge.py`) scores the version with and without the teaser and keeps the better one
  (use the same blind A/B method; the simpler version wins ties).
- Skip for clips whose payoff is a surprise that the teaser would spoil (Claude says so in `why`).

### 1B. Short inserts in the middle of a clip
Three kinds:

| Kind | What it is | Audio |
|---|---|---|
| **Proof** | When he says "I made $50k today" / "look at this chart", cut to the 1–3 s where the P&L / chart / result is actually on screen (from elsewhere in the same video). | Main clip's audio keeps playing (video-only insert, "B-roll" style) |
| **Reaction** | 0.7–2 s of a real reaction to *this same moment* (another person in the room, chat on screen, his own face right after). | Main audio keeps playing, or the reaction's own audio if it's a laugh/shout |
| **Callback** | 1.5–4 s of an earlier line that the current line refers back to ("remember I said I'd never…"). | Its own audio (a real cut in and back out) |

How to find them:
- **Proof:** trigger words in the transcript ("look at", "right here", "my account", "P&L", "this chart", "watch this",
  numbers with $ / k / %). For each trigger, grab frames around it and around matching moments elsewhere in the video
  (sample every 2 s within the video's screen-share stretches: no face + lots of on-screen text/UI is a good sign — use
  `motion.analyse` face data and frame differencing), then ask Claude with 4–8 frames: "Which frame range shows what
  he's talking about? None is fine."
- **Reaction / callback:** from the transcript: Claude marks lines that refer back to earlier lines, and laughs/shouts
  (word timestamps + loudness spikes; transcript "[laughter]" tags when present).
- Max 3 inserts per clip, never in the first 1.5 s (the hook) or over the punchline itself.

Rendering:
- Callbacks are just extra parts (the existing `parts` / `segments` route). Allow a payoff part to be split around a
  callback (relax `structure.stitch_problem` for role `callback`).
- Proof / reaction inserts with the main audio running need **separate video and audio timelines**: the audio follows
  the main parts; the video swaps to the insert's frames for its duration. In `motion.py`, add an optional
  `video_overrides: [(out_start, out_end, src_start)]` applied to the frame index map (`Timeline.src_index`) while
  `_audio_graph` keeps using the unmodified segments. Inserts get their own framing (usually "fit" with blurred
  background for screen shares, so the chart is readable — don't crop a chart to a face crop).
- Captions keep following the audio (the main speaker's words), so they don't change during a video-only insert.

### 1C. Stitch funny and hype moments when it helps
Today `structure.SYSTEM` says "Funny and hype moments carry themselves: never stitch them." Change to: stitch a funny or
hype moment **only** when its setup, an earlier mention, or a running joke sits elsewhere in the video and makes the
punchline land harder; keep it tight (≤ 35 s, punchline untouched, ends on the laugh). The judge still decides between
the straight and stitched versions.

### 1D. Interface (Part 1)
- More options: "Open with a teaser of the best part" (Decide / Always / Never) and "Add proof shots and reactions"
  (on by default).
- Editor: the trim timeline shows teaser / inserts as coloured blocks; each can be removed with one click; the Ask tab
  understands "remove the teaser", "add a teaser", "show the chart when he says 50k", "no inserts".
- `instruct.py` gets these as new controls.
- Clip card: small tags "Teaser" / "2 inserts" so gs sees what was done.

### 1E. Tests (Part 1)
- Teaser placement: inside payoff, 1.2–3.5 s, payoff still complete afterwards, labels at the right times.
- Video-only insert: audio timeline unchanged (same length, same words), video frames come from the insert range.
- Quote check rejects a part whose words don't match the transcript.
- Judge picks without-teaser on a tie.
- Funny moment stitch only accepted when a setup/callback part exists.

---

## Part 2 — across videos

### 2A. Where the videos come from
1. **A batch** (several links pasted in Make): new toggle "Also find clips that combine these videos" (on by default
   for 3+ links).
2. **A campaign:** button on the campaign page "Find stories across this campaign's videos" (all finished jobs of the
   campaign, optionally pick which).
3. **Any selection in My videos:** select 2+ videos → "Combine into stories".
4. **Creator Scan** (Part 3).

All of these already have transcripts with word times (cached per source) and the source files in `data/sources`.

### 2B. Finding parts that belong together (Claude judges on its own)
1. **Video maps** (cheap, once per video, cached): Claude (screening model `CLAUDE_SCREEN_MODEL`, falls back to
   `CLAUDE_MODEL`) reads each transcript and writes a compact map: topics, stories, claims, predictions, numbers,
   dates/time words, people, catchphrases, questions asked, strong moments — each with start/end times and the exact
   words. Store in a new `video_maps` table (job_id, map JSON, model, created_at).
2. **Thread finding:** Claude (`CLAUDE_MODEL`) reads the maps of all selected videos (plus their upload dates and
   titles) and proposes **threads** — sets of parts from 2+ videos that make one clip. Thread types:
   - **Then vs now** — the same thing at two points in time ("blew my first account" → "first $100k day").
   - **Prediction → result** — he calls something in one video; another video shows what happened.
   - **Question → answer** — asked in one video, answered in another.
   - **Same advice, many times** — "his #1 rule, said 5 times" (short supercut, 3–6 pieces).
   - **Running joke / catchphrase** supercut.
   - **Reaction** — him reacting in video B to what happened in video A (only when B really is about A).
   - **Contrast** — two different takes on the same topic (careful with rule 0.1 — both must be his real, complete
     points).
   - **Story across videos** — beginning in one, middle in another, ending in a third.
   For each thread: parts `[{job_id, start, end, role, label, quote}]` in play order, a hook (≤ 8 words, understandable
   cold), a one-line "why this works", and the thread type.
   - Many videos (> ~30): group by topic tags from the maps first, then run thread finding per group.
3. **Check:** rule 0 checks in code (quotes, complete sentences, true labels from upload dates, length cap), then the
   campaign rules via the rulebook.
4. **Judge:** `judge.py` scores the cross-video candidates on the same scale as normal clips (cold viewer: clarity,
   hook, context, payoff, flow, pace), so they're ranked together with the single-video clips. Keep the top N
   (gs sets, default 5). Also extend `CRITERIA["flow"]`: jumps between videos must feel motivated and be marked.

### 2C. Building the clip (reuse the whole pipeline)
Recommended approach — a **joined source**:
1. For each part, cut it from its own source with ±1 s padding (ffmpeg, accurate seek).
2. Normalise every piece to one format: 1920×1080 canvas at 30 fps (a vertical source goes in the middle over a blurred
   copy of itself so framing still finds the face), audio 48 kHz stereo, each piece loudness-normalised so levels match;
   optional light colour match (mean/contrast of the luma plane) so the pieces look like one film.
3. Concatenate into `data/sources/mix_<clip>/source.mp4` and write `manifest.json`: for every piece, the original
   job_id, source path, original start/end, and where it sits in the joined file.
4. Re-time the words of each part onto the joined file's clock and build normal `parts` on it (with the labels).
5. Make the clip with the existing route: a clip with its own `source_path` (pipeline already supports
   `clip["source_path"]`) → `build_parts` → `render.render_clip` (seamless engine) → clip doctor → campaign gate.
6. **Framing per part:** different videos need different layouts (Kick stream with a facecam vs a YouTube talking
   head). Decide framing per manifest piece (scene cut at every piece boundary; the engine already handles scene cuts),
   not once per clip.
- Data: the clip belongs to a new job of `type: "mix"` with `sources` (JSON list of job ids). Each part in
  `clips.parts` gets `job_id` too. The editor can re-render from the joined source; if a part's times change, rebuild
  the joined source from the manifest.
- Time-jump labels between videos use the upload dates: same year → "2 MONTHS LATER"; different years → "2023" /
  "2026"; unknown → "LATER". Optional small "from: <video title>" line (setting, off by default).

### 2D. Interface (Part 2)
- Video page of a batch / campaign page / My videos selection: a section **"Stories across videos"** with the cross
  clips. Each card shows its thread type ("Then vs now", "Prediction → result"…) and the strip of where each part comes
  from, coloured per video with the video titles.
- Progress in plain words: "Reading 6 videos · Looking for stories that connect them · Found 4 · Making clips 2 of 4".
- Cost/time estimate before starting when more than ~10 videos are selected.
- Editor: remove a part, swap the order, or "use a different ending" (Ask tab → Claude picks another part from the
  thread's videos).
- Telegram: "2 new story clips combining 3 TJR videos — want them?"

### 2E. API (sketch)
`POST /api/stories` (body: job_ids or batch_id or campaign_id, how many, settings) → mix job id ·
`GET /api/stories/{job_id}` (threads + clips + progress) · `POST /api/clips/{id}/parts` (editor: remove/reorder/replace
a part).

### 2F. Tests (Part 2)
- Video map + thread parsing from stubbed Claude replies; quotes that don't match the transcript are dropped.
- Labels from upload dates (same year, different years, unknown).
- Joined source: piece times → joined file → words re-timed exactly; loudness of pieces within ±1 LU.
- A misleading stitch (a reaction placed after something else; a sentence cut mid-way) is rejected by the checks.
- A mix clip goes through the clip doctor and the campaign gate like any clip.
- API endpoints used by the interface.

---

## Part 3 — Creator Scan (after `CREATOR_SCAN_PLAN.md` is built)
- Run thread finding over the creator's catalog: maps come from `catalog_words`; parts come from the fetched sections
  (`moments.section_path`); fetch extra sections on demand when a thread needs a part that wasn't downloaded.
- Creators page: "Find stories across his catalog" and a search box ("his first account → now") that returns threads.
- Proof inserts may come from other videos too (e.g., the P&L screen from another stream), still following rule 0.

---

## Done means
- **Part 1:** on a real TJR video, ClipAgent makes clips where some open with a teaser, some show the chart/P&L when
  he talks about it, and gs can remove either with one click; tests pass; frames checked by eye.
- **Part 2:** gs pastes 3–6 TJR links (or picks the TJR campaign) and gets a few "Stories across videos" clips that
  really connect (e.g., then vs now), with true labels, matched sound levels, correct framing per part — and every one
  passes the clip doctor and the campaign gate.
- **Part 3:** the same works across the whole catalog from the Creators page.
