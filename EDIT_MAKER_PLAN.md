# Edit Maker — full spec and build steps

**Goal.** Inside ClipAgent, make *edits* (not just clips): short, music-driven videos built from several moments of a
creator's videos, cut to the beat of a song, with the effects viral edits use — speed ramps and slow-mo, flashes, zoom
punches, shakes, RGB glitch, colour grades, grain, cinema bars, a text hook, words on screen, and an ending that loops.
It must follow the recipe in `docs/VIRAL_EDITS_RESEARCH.md`, obey campaign rules, and be usable by gs without editing
skills: pick footage, pick a style, pick a song, press Make edit, then tweak with simple controls or plain words.

Read `CLAUDE.md` first (rules, how to run, tests).

---

## 0. What already exists (keep, finish, don't rewrite blindly)

| File | State |
|---|---|
| `app/beats.py` | **Done, tested** (`tests/beats_detect.py`). `analyze(path)` → `{duration, bpm, beats[], bars[], drop, energy[]}`. Spectral-flux onsets, autocorrelation tempo with half-tempo correction, Ellis DP beat tracker, bar phase, drop = biggest sustained loudness rise. numpy only. |
| `app/edits.py` | **Draft.** STYLES, EFFECTS, sounds helpers (`add_sound`, `sound_json`), footage list (`usable_sources`), Claude moment picker (`pick_moments`, `PICK_TOOL`, `PICK_SYSTEM`), `snap_moments`, `build_timeline` (beat pace + speech pace), `create`, `run`, `remake`, `edit_json`. Not wired into main.py. `run()` calls `editrender.render(...)`, which doesn't exist yet. Review it against this spec; fix what disagrees. |
| `app/store.py` | Tables `sounds` and `edits` + helpers (`add_sound`, `get_sound`, `list_sounds`, `delete_sound`, `create_edit`, `update_edit`, `get_edit`, `list_edits`, `delete_edit`). |
| `tools/make_test_song.py` | Makes royalty-free test songs with a known tempo and drop. |

Footage = any finished video in My videos whose source is still in `data/sources` and has a transcript
(`edits.usable_sources()`). Transcripts are reused — an edit never re-transcribes.

---

## 1. The styles (what gs picks)

Each style is a preset of pacing, audio mix, grade, effects and text. All are tweakable after.

| Style | Pacing | Audio | Grade | Effects (default on) | Text | Typical length |
|---|---|---|---|---|---|---|
| **Velocity** (was "Hype") | cut on the beat: every 2 beats in the build, every beat (or 2 at fast tempos) after the drop; best moment on the drop | music only | punchy (contrast up, saturation up, crushed blacks) | speed ramps, slow-mo on the drop, flash on bar-line cuts + drop, zoom punch on every beat, shake + RGB glitch on the drop, vignette | hook (lore/claim) first 3 s, then 1–5 punch words per moment | 15–25 s |
| **Aura** | slow: a cut every 2 bars; long slow-mo holds | music only (phonk/funk) | mono or teal-dark | heavy slow-mo (0.5–0.6×), slow push-in, grain, vignette, flash on the drop only | lore hook ("bro made $20M at 24 and still…"), nothing else | 10–20 s |
| **Flow** (match cuts) | ~1 cut per beat, each cut mid-movement into a shot whose motion continues it | music only | one unified grade (teal/orange) | motion-matched cuts, directional motion blur on cut frames, small zoom-in + shake on hits, flash on the biggest hits only | hook only | 20–35 s |
| **Cinematic** | the speech decides; cuts snapped to the next beat | voice 100% + music ~30% | film (teal shadows, warm highlights, soft contrast) | slow push-in, cinema bars, grain, vignette, dip to black between moments | subtitles of what he says (lower third) | 25–40 s |
| **Motivation** | speech | voice + music ~36% swelling | black & white, high contrast | push-in, grain, vignette, flash on the drop | the line builds word by word, big, centred; key word yellow | 15–30 s |
| **Funny** | speech, hard cuts | voice + light music bed (optional) | none | zoom punch + shake on each punchline | meme caption on top (viewer's voice) | 20–40 s |
| **Money** | a cut every 4 beats | music only | warm gold | slow-mo 0.8×, push-in, dips, grain | short money lines | 15–25 s |

Map to TJR: Velocity/Aura for wins, money, "success" moments; Motivation for his trading-psychology lines; Funny for
reactions and banter; Money for the lifestyle vlogs (Italy, St Tropez, Ibiza weeks).

Every style also gets, from the research recipe:
- **Hook in the first ~3 s** (Claude writes it from what's actually said — lore or claim, never a vague teaser).
- **Loop ending**: end on a bar line; the last ~0.4 s cross-dissolves into the edit's first frame (and the music is cut on
  a bar so the audio loops too). Toggle "Loop the ending" (default on).
- **Length 10–35 s** by default (choices 15 / 20 / 25 / 30 / 40 / 60).

---

## 2. Songs ("Sounds")

- gs adds songs he has the rights to use (upload MP3/WAV/M4A, or a video file — use its audio). ClipAgent must NOT
  download songs from YouTube/TikTok itself. Say on the page: "Use songs you're allowed to use. On TikTok/Reels you can
  also add the trending sound in the app after posting."
- On upload: `beats.analyze()` once; store in `sounds` (file in `data/sounds/`). Show name, BPM, length, a little
  energy curve with the drop marked, a play button.
- Song section: the edit starts N beats before the drop (Velocity 8, Money/Aura 4; speech styles: whatever lines the
  drop up with the strongest moment) and runs for the chosen length, ending on a bar line.
- Optional "Pick the part of the song" control: drag the start on the energy curve (default = auto).
- "No music" is allowed for speech styles (Cinematic, Motivation, Funny) — then cuts aren't beat-snapped.
- Built-in demo beats: only ones generated by ClipAgent itself (tools/make_test_song.py style), labelled "Demo beat".

---

## 3. Picking the moments (Claude)

`edits.pick_moments(sources, style, theme, length, guidance)` — one Claude call with the transcripts (timestamped
lines; the moments ClipAgent already rated highly for each video listed first; ~90k characters total budget split
across videos). Tool `pick_moments` returns: title, caption, hashtags, and moments in play order:
`{video, start, end, hit, text, kind, drop, why}`.

Rules in the prompt (already drafted in `PICK_SYSTEM` + each style's `brief`):
- only real moments (transcript times), sentence edges for voice styles (then `snap_moments` uses
  `highlights.clean_bounds`);
- text only from what's said (or a viewer-voice caption for Funny); never make the person look bad;
- order: stop-the-scroll opener → build → strongest on the drop → a line that sticks at the end;
- campaign guidance (`campaign.picker_guidance(rules)`) appended when the edit is for a campaign; the campaign's
  required hashtags are added to the post hashtags.
- Add: a `hook` field (the first-3-seconds text) to the tool.
- For **Flow**, also return 15–30 short "action" moments (1–3 s) — motion matters more than words; see §5.

Moment count: Velocity 8–12, Aura 4–7, Flow 15–30, Cinematic 3–5, Motivation 2–4, Funny 4–7, Money 6–9.
Later, Creator Scan's moment library (`docs/CREATOR_SCAN_PLAN.md`) feeds this directly ("best moments across his
whole channel") — design `pick_moments` so it can take pre-scored moments instead of raw transcripts.

---

## 4. The timeline (`edits.build_timeline`)

Output (stored in `edits.plan.timeline`, JSON):
```
{style, length, voice (0–1.5), effects{...},
 music: {sound, start (song seconds), drop_at (edit seconds), level} | null,
 segments: [{moment, source(job id), src_start, speed | speed_curve, at, dur, beats[] (relative), drop, flash,
             shake, glitch, hit (relative), voice_until, text, first, words[{w,t,end}] (edit time)}]}
```
- **Beat pace** (Velocity, Aura, Flow, Money): beat grid from the song section; cut list = every `build_cut` beats
  before the drop and `cut` beats after, never cutting across the drop (land a cut exactly on it); moments fill slots in
  order, the drop moment starts exactly on the drop with its `hit` aligned to it; a moment can span several slots
  (continuous footage with zoom punches on the inner beats). When moments run out, continue the same moment further on.
- **Speech pace** (Cinematic, Motivation, Funny): each moment plays whole at 1×; with music, the song is placed so its
  drop meets the drop moment, and each cut is pushed to the next beat (the picture holds a little longer; the voice is
  faded out at `voice_until` so the next sentence never leaks in).
- **Speed ramps** (Velocity/Flow): per segment a speed curve, not one number: fast in (1.5–1.8×) → slow on the hit
  (0.35–0.5×) → fast out, normalised so the source time used fits the moment. Source time = integral of speed.
- Keep `build_timeline` pure (no I/O) so it's easy to test.

---

## 5. Flow edits: matching motion (new module `app/motionmatch.py`)

1. For each candidate shot (moment), sample 6–10 frames, compute dense optical flow (OpenCV
   `cv2.DISOpticalFlow_create(cv2.DISOPTICAL_FLOW_PRESET_FAST)` on small grey frames) → per shot: dominant direction at the
   start and at the end (mean flow vector), speed, zoom (divergence), rotation (curl), where the subject is (face centre).
2. Cost of cutting from shot A's end into shot B's start = direction mismatch + speed mismatch + subject-position
   jump. Chain shots greedily (or a small beam search) to minimise total cost; cut each shot at its peak of motion.
3. On cut frames add directional motion blur along the motion vector (average 3–5 warps) to hide the cut.
4. Netflix's match-cut research (github.com/Netflix/matchcut) is the reference for the idea; don't add heavy models
   (no CLIP download without gs's OK — rule 3).

---

## 6. The renderer (new module `app/editrender.py`)

`render(timeline, sources_by_id, sound, out_path, thumb_path, progress=cb)`. Two stages, one final encode.

**Stage A — picture, frame by frame in Python (OpenCV, BGR24, 1080×1920, 30 fps):**
- For each segment: decode the source range with ffmpeg as raw frames (see `motion._Decoder` for an exact-seek
  reader; native size). Output frame j of a segment maps to source time via the speed curve; pick the nearest source
  frame, or blend the two neighbours for slow-mo; optional quality mode: optical-flow interpolation with DIS for
  Twixtor-like slow-mo (only for slow sections; slower).
- **Framing**: crop a 9:16 window around the subject: `framing.sample_faces(source, s, e, count=5)` → largest face's
  median centre (fallback centre). One affine per frame does crop + scale + zoom + shake (sub-pixel, `cv2.warpAffine`
  INTER_LINEAR; see `motion._affine`). Portrait sources: scale to fill.
- **Zoom**: Z(t) = 1 + push·t/dur + Σ_beats A·e^(−k(t−b))·[t≥b] (A≈0.06–0.09, k≈9) + punch on the hit (Funny:
  1.0→1.22 in 80 ms, ease back over 400 ms). Zoom about the face centre.
- **Shake**: offsets x = S·e^(−6(t−h))·sin(2π·13(t−h)), y with a different frequency, tiny rotation; S≈18 px.
- **Flash**: white overlay alpha = 0.85·e^(−14t) from the cut (cv2.addWeighted with white).
- **RGB glitch**: for 0.12 s at the drop shift B and R channels ±8–14 px in opposite directions.
- **Grade**: per-style 256-entry LUT per channel (`cv2.LUT`, fast): punchy, film (teal/orange), mono, gold, none. Build
  LUTs from simple curves (lift/gamma/gain per channel + saturation via a small HSV step if needed).
- **Dips** (Cinematic/Motivation/Money): fade the last 0.15 s to black and the next 0.15 s from black.
- **Loop**: last 0.4 s cross-dissolve into the edit's first frame (keep frame 0 in memory).
- Pipe frames into the Stage B ffmpeg process (stdin rawvideo).

**Stage B — one ffmpeg command:** rawvideo from stdin + voice track + song section →
- video filters: `vignette`, `noise=alls=7:allf=t` (grain), `drawbox` cinema bars (keep text out of the bars' safe
  zone), `subtitles=<edit.ass>:fontsdir=<fonts>` (escape Windows paths like `motion._escape`), `format=yuv420p`;
- audio: voice (built first: each segment's source audio trimmed, `atempo` for speed changes on voiced segments,
  faded at `voice_until`, padded to exactly `dur`, concatenated) × voice level + song `-ss start -t length` with a
  20 ms fade-in × music level → `amix=normalize=0` → `alimiter` → `loudnorm=I=-14:TP=-1.5`;
- encode libx264 `-preset veryfast -crf 19`, AAC 192k, `-movflags +faststart`; thumbnail = the drop frame.

**Text (ASS, PlayRes 1080×1920, fonts Anton / Poppins from `fonts/`):**
- hook: first ~2.8 s, Anton ~92 px, white with black outline, top third (y≈420), pop-in;
- punch words (Velocity/Money): Anton 110–120 px centred ~62% height, pop-in on the cut (`\t` scale 112%→100%);
- subtitles (Cinematic): Poppins Bold 54 px, phrase by phrase in time with the words, lower third above y=1520;
- build (Motivation): the whole line laid out once; each word becomes visible when spoken (`{\alpha&HFF&}` on words
  not yet said) so the layout never jumps; key word yellow;
- meme (Funny): Anton 76 px top caption for the moment.
- Respect safe zones (top 190, bottom 400, right 140).

Speed target on the PC (i7-1255U): a 20 s edit in under ~60 s. Profile; if slow, decode at half size for sources above
1080p and use INTER_LINEAR.

---

## 7. Campaign rules

- An edit can be "for a campaign". Then: the campaign's guidance goes to Claude; required hashtags are added; logos
  never; and if the rulebook doesn't allow **music** (`campaign.allowed(rules, "music")`), refuse a song with a clear
  message: "<campaign>: the brief doesn't allow added music. If it does, switch music on in the campaign's rules."
  (TJR's brief doesn't mention music, so it counts as not allowed until gs allows it.)
- Run edits through `compliance` the same way clips are (length, hashtags, logos) before Download.
- Edits are transformative by construction (structure, text, grade, cuts) — keep it that way (Instagram originality rule).

---

## 8. API (add to `main.py`)

- `GET /api/sounds` · `POST /api/sounds` (multipart file + name; analyse; returns sound) · `DELETE /api/sounds/{id}`
  · `GET /media/sound/{id}` (stream the file for the play button)
- `GET /api/edit-sources` → `edits.usable_sources()`
- `GET /api/edit-styles` → styles (name, what, default effects, length) + effect labels
- `POST /api/edits` json `{style, sources[], sound, theme, length, effects{}, campaign_id}` → `edits.create` → `{id}`;
  400 with plain words on bad input
- `GET /api/edits` · `GET /api/edits/{id}` (`edit_json`) · `DELETE /api/edits/{id}` (remove files too)
- `POST /api/edits/{id}/remake` json `{style?, sound?, length?, effects?, voice?, music?, theme?, moments?[{id, text?,
  off?, drop?}] in new order, repick?}` → `edits.remake`
- `POST /api/edits/{id}/ask` — typed change requests for edits ("faster", "more flashes", "black and white", "put the
  $20M line on the drop", "use a different song") → Claude maps to the remake fields (same pattern as `instruct.py`)
- `GET /media/edit/{name}` (mp4 + jpg) · `GET /api/edits/{id}/download`
- Telegram: send the finished edit (`notify.send_video`), and `/edit <style> <theme>` makes an edit from the last
  finished video with the last-used song.

---

## 9. Interface

Sidebar: add **Edits** (between My videos and Campaigns). Route `#/edits` and `#/edit/<id>`.

**Edits page — "Make an edit"** (same card/step look as Make clips):
1. **Footage** — cards of usable videos (poster, title, length), tick one or more; hint: "Add a video in Make clips
   first — ClipAgent reuses its words, no new download."
2. **Style** — gallery of the 7 styles with an animated CSS sample each (like the look gallery) and one line each.
3. **Song** — list (name, BPM, length, energy curve with the drop, play) + "Add a song" upload + "No music" (speech
   styles only); the rights note.
4. **About** (optional) — "What's it about?" ("his best trading advice", "funniest moments", "money and wins"),
   length chips, campaign select (shows the music rule).
5. **Make edit** → goes to the edit page. Below: your edits (poster, style, length, status).

**Edit page:** big 9:16 preview + Download + Copy caption; progress steps while making ("Picking the moments",
"Fitting it to the beat", "Rendering 40%"); controls: style chips, song select, length, effect toggles (the 10
effects in `edits.EFFECTS` + "Loop the ending"), voice/music sliders; **moments list** (thumbnail, the text — editable,
source title, length, drop marker, on/off switch, drag or up/down to reorder); "Re-make" and "New moments" buttons;
a "Tell ClipAgent what to change" box. Phone layout like the clip editor (one column, preview first).

---

## 10. Tests (write these, keep them fast and offline)

- `tests/beats_detect.py` (exists).
- `tests/edit_timeline.py`: with a fake song analysis (beats at 128 BPM, drop at 20.99 s): Velocity cuts land on beats,
  a cut lands exactly on the drop, the drop moment's hit is on the drop, total length = requested; speech pace keeps
  every moment whole and snaps cuts to beats; turning moments off / reordering works; no song → clear error for beat
  styles.
- `tests/edit_render.py`: render a 6 s edit from a synthetic source (`ffmpeg -f lavfi testsrc2`) + a test song; check
  the output is 1080×1920, 30 fps, length ±0.1 s, has audio, loudness ≈ −14 LUFS (ffmpeg `ebur128`), a flash frame is
  bright, the glitch frame has shifted channels. Then pull frames and look at them.
- `tests/edit_api.py`: endpoints with Claude stubbed (like `tests/ask_changes.py` does), campaign music rule refusal.

## 11. Done means
- gs can make a Velocity edit of TJR from 3 videos and a song in under ~2 minutes, with cuts on the beat, the best line
  on the drop, flashes/zooms/shake/glitch where they belong, a hook at the start, and a clean loop.
- All 7 styles render; every effect toggle visibly works; re-make without Claude is fast; typed changes work.
- Campaign rules are enforced; nothing else in ClipAgent broke (run every fast test suite).
