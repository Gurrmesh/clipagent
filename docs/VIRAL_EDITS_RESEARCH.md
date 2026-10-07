# Viral edits research (Sept 2026) — the basis for the Edit Maker

Source: gs's research session of 27 Sept 2026 (vidIQ pulls of IG/TikTok since late June 2026 and YouTube Shorts since
Sept 2025, plus web research). Use this as the reference for what the Edit Maker must be able to make.

## The one-line summary
The edits pulling millions are **10–35 second, beat-synced "aura" edits**: one character / athlete / streamer, cut to a
slowed Brazilian funk ("montagem") or phonk track, with a **text hook** on top and an ending that **loops** back to the
start. Accounts under 5K followers hit 1–7M views with them (100–1,800× their usual). The songs themselves get 20–80M.

## 1. What goes viral (examples)
- **Movie/TV "aura" edits** — Iron Man suit-up to phonk (2.5M, 590-follower account), Yondu "farming too much aura"
  (2M), "most aura thing in Marvel" (4.8M), Peaky Blinders sigma edit (8.1M), Spider-Verse (7.1M), Cobra Kai (14.8M),
  "Bro is farming aura" Krrish 3 (16.2M on a 38K-sub channel). Archetype: "literally me" characters (Joker, Driver,
  Officer K, Patrick Bateman, Guts).
- **Football** — Mbappé/Ronaldo (6.4M), Messi × Thorfinn (crossover), Ronaldo × Interstellar monologue,
  "Dictator Mbappé", Man City × Roblox avatars.
- **Anime** — Death Note, Mikasa, Haikyuu rankings, Demon Slayer (one at 1,800× the account's usual), JJK made in free
  DaVinci Resolve (5.8M).
- **Brainrot / meme** — 8–11 second edits pulling 10–25M (Brazilian phonk be like, Minecraft phonk, troll-face, FNAF).
- **Streamers** — Jynxzi "mogging" edit (2.6M), IShowSpeed turned into a funk track (11.9M).
- Songs: Montagem Pegadora (slowed) 78M, Montagem Alquimia (slowed) 61M, Bad Ending Funk 27M, Montagem Fearless 24M,
  Homage Funk 20M. Trending in Aug–Sept 2026: Re:Zero/Subaru + Montagem Oblivion, MTG Prism edits, "aura battles".

## 2. How they're made — the recipe (same in every genre)
1. **Song first; mark its beats.**
2. **8–20 clips; the best shot goes on the drop.**
3. **Velocity**: speed ramps (fast in → slow on the hit → fast out) plus smooth slow-mo (Twixtor-style retiming).
4. **Shakes, flashes and zoom punches on the beats**, then **colour correction ("CC")** for the "4K" look: deep blacks,
   contrast, slight teal/orange or a unified grade so every source looks like one film.
5. **Open with a text hook** in the first ~3 s: lore ("bro was chained up for 6 years and still…") or a claim
   ("most aura thing in Marvel").
6. **End on a frame that flows back into the first** so it loops invisibly (rewatches).

Flow / match-cut edits (the "infinite transitions", 無限轉場 / 丝滑转场 style): ~1 cut per second on the beat, every cut
lands mid-movement and the next clip continues the same movement (direction, spin, zoom), grouped by character, one
grade over everything, white flash only on the biggest hits, small shake + zoom-in on each hit. The automatable part:
tag every shot by its motion (direction, speed, zoom, where the subject sits) and chain shots whose motion continues —
Netflix's open research on match cutting (github.com/Netflix/matchcut, CLIP-based shot similarity) shows the method:
score every pair of shots for how well they match, then chain the best ones.

Tools editors use: CapCut (auto beat-sync, auto-velocity), Alight Motion (mobile AE; QR/XML presets), After Effects
(Twixtor, shake presets, glow), DaVinci Resolve (free), Instagram's Edits app; Flowframes/Topaz for interpolation and
upscaling. Slang: ib = inspired by, scp = scenepack, cc = colour correction, velo = velocity, tut = tutorial.

Tutorials worth reading the structure of: Full Velocity Edit Tutorial (AE) youtube.com/watch?v=9mmYE-jLg2w ·
How I twixtor my velocity edits youtube.com/watch?v=0dXRvkGdcc4 · How I Made This Edit youtube.com/watch?v=P14sJ5Nuucs ·
Wizzy editing process youtube.com/watch?v=pLMHPbu3WCU · CapCut shake tutorial youtube.com/watch?v=mzJSoHOsTQw ·
Learn By Leo (retention theory) youtube.com/watch?v=sLgHqZSe2o0 · How to make flow edits youtube.com/watch?v=_BQhKA8qsHk

## 3. Clips and audio
- **Scenepacks** = folders of pre-cut clips (5–60 s, no subtitles/watermarks) from YouTube descriptions, editpacks.org,
  Veel, Suits, YouPro, Discord/Telegram, Gumroad. 1080p is the working standard, 4K for crops/zooms.
- **Clip-finding is the bottleneck** — packs are manual and links die. → ClipAgent's Creator Scan is effectively an
  automatic scenepack builder for a creator (see `CREATOR_SCAN_PLAN.md`).
- **Audio**: funk producers release "slowed" / "super slowed" versions for edits; TikTok Creative Center → Trends →
  Songs; sounds peak within 5–14 days of breaking out — catch them while climbing.

## 4. Why they go viral
- Short and looped: most hits run 10–35 s and loop. Watch-time matters most (an OpusClip study of 500 videos: ~70%
  average watch time got 4.3× the impressions of ~40%).
- "Aura" framing (the subject constantly looking cool), crossovers (two worlds in one edit), followers barely matter.
- Instagram's top signals: watch time, DM sends per reach, likes per reach.

## 5. Money and risk
- Whop Content Rewards $0.20–$6 per 1K (tracked payouts average ~$0.39/1K after caps/filters); Kick pays clippers
  ~$40–50 per 100K views; labels run ~$2/1K song campaigns.
- **Copyright**: transformative edits usually survive on TikTok/Reels; monetised YouTube uploads get Content ID claims.
- **YouTube** "inauthentic content" policy (July 2025) — substantive edited effects on reused footage count as unique;
  a Jan 2026 wave demonetised channels with ~35M combined subs.
- **Instagram** (since 30 Apr 2026): accounts that mostly repost others' content without meaningful transformation
  aren't recommended to non-followers; borders, watermarks and speed changes alone don't count as transformation.
  → The Edit Maker must make real edits (structure, text, cuts, grade), never just re-upload footage.
- Third-party "upload methods" for 4K/120fps carry ban risk — ClipAgent must not use them.
- Campaign briefs can forbid music, logos or "AI-generated" content — the Edit Maker obeys the rulebook.

## 6. Gaps worth building into
- No tool ties editing to the song's timing automatically end to end (auto-editors only add effects on beats).
- Clip finding (packs) is manual — ClipAgent can find the moments itself from transcripts, audio and "most replayed".
