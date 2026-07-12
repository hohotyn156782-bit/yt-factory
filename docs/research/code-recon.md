All code read and verified against the actual sources. Report follows.

# content-factory reuse audit for EN/US YouTube factory (long-form 16:9 + Shorts)

Existing system: Russian vertical 30–35s Shorts, 1080x1920@30, built around `core.W/H/FPS`, `niches.json` registry, GitHub Actions autopilot. Total ~10.4k lines. Overall: the **infrastructure layer is highly reusable** (LLM cascade, secrets, cooldowns, topics DB, trends parser, YouTube adapter, CI skeleton); the **format layer is vertical/short-form hardcoded** (resolution, subtitle geometry, script length gates, QA duration window, thumbnails).

## Per-module verdicts

### core.py (600 loc) — **copy-with-edits**
- (a) Foundation: secrets loading (`load_local_secrets`, `secret`, log-scrubbing of secret values), niche registry (`load_niches`/`get_niche`), append-only `history.jsonl`, JSON logger with daily gzip rotation, `run`/`run_retry` (ffmpeg wrappers), `http_json`/`http_download` with backoff, disk guards (`check_disk`, cleanups), flock, persisted cooldown maps (`load_cooldown`/`save_cooldown`), heartbeat, healthchecks.io pings, `sanitize_external` (anti-prompt-injection), `slugify` with RU translit.
- (b) Hardcodes: `W, H, FPS = 1080, 1920, 30` (line 72) — the single most-imported constant (broll, assemble, qa, imagegen, thumbnail all read it); `TZ = UTC+3` MSK; `FONT_BOLD` DejaVu; data root `/mnt/d/content-factory-data` (overridable via `CF_DATA_ROOT`); secrets paths `~/.config/content-factory/secrets.env` + inherits `~/.config/content-engine/secrets.env`.
- (c) Edits: make W/H/FPS per-format (e.g. a `FORMATS = {"short": (1080,1920), "long": (1920,1080)}` dict or per-niche field), change TZ to US-relevant, change secrets dir name. Everything else copies verbatim.

### pipeline/llm.py + llm_providers.json — **copy-as-is**
- (a) `llm.chat(system, user, json_mode, max_tokens, temp) -> str`. OpenAI-compatible cascade, multiple keys per provider via comma-separated env, per-key opaque-sha1 cooldown persisted to disk (survives short cron runs), 429/quota → next key → next provider, Retry-After honored, empty-content (reasoning models) treated as transient.
- (b) Fully language-agnostic. Cascade order was tuned for RU quality (comments say "Mistral Large — топ RU-качество"); for EN you may prefer gpt-4o/gemini first, but it works untouched. `max_tokens` default 900 is short-form-sized — long-form scripts need callers to pass 4000+.
- (c) Copy both files; optionally reorder providers.

**Cascade (priority order, enabled) with env key names:**
| # | name | model | key env |
|---|------|-------|---------|
| 1 | github-models | openai/gpt-4o | `GITHUB_MODELS_TOKEN` |
| 2 | mistral | mistral-large-latest | `MISTRAL_API_KEY` |
| 3 | gemini | gemini-2.5-flash | `GEMINI_API_KEY` (comma-multi) |
| 4 | gemini-lite | gemini-2.5-flash-lite | `GEMINI_API_KEY` |
| 5 | cerebras | qwen-3-32b | `CEREBRAS_API_KEY` |
| 6 | groq | openai/gpt-oss-120b | `GROQ_API_KEY` (shared with lead-hunter bots) |
| 7–9 | openrouter ×3 | qwen3-80b / gemma-4-26b / nemotron-3 (:free) | `OPENROUTER_API_KEY` |
| 10 | chutes | Qwen3-32B | `CHUTES_API_KEY` |
| 11 | pollinations | "openai" | **no key** (`no_key: true`, last-resort) |
| off | nvidia-nim | deepseek-v4-flash | `NVIDIA_API_KEY` — deliberately disabled for text, key reserved for FLUX images |
| off | xai-grok | grok-3-mini | `XAI_API_KEY` |

### pipeline/script.py (999 loc) — **copy-with-edits for EN Shorts; REWRITE for long-form**
- (a) `generate(niche, topic, avoid, serial, platform_hint, target_words) -> script dict` (hook / 4-6 segments with English `broll_query` per segment / outro / title / description / caption / hashtags / thumb_text). Self-improvement loop: generate → `_polish` editor pass → `_trim_to_words` → `validate()` mechanical gate → loop-echo gate (hook↔outro shared words) → segment-1 concreteness gate → atomic topic reservation (`topics_db.reserve_topic`) → `_three_hooks` A/B (deterministic hook scoring) → `_virality_score` LLM judge (6 axes ×20, threshold `VIRALITY_MIN=90/120`, `MAX_SCRIPT_ATTEMPTS=8`). Also `ban_risk()` LLM moderation (fail-open), `to_chunks()`, `validate()`.
- (b) Breakages for EN/long: **word gates 62–92** (`validate` line 914), `target_words` clamped 66–90, trim max ~92 → physically caps output at ~35–40s; **EN prompt variants already exist** (`ANTI_SLOP_EN`, `HOOKS_EN`, `STRUCT_EN`, `STORY_*_EN`, `AVOID_EN`) selected by `niche["lang"]=="en"`, BUT the surrounding system-prompt scaffold (schema descriptions, format rules, `_polish`/`_three_hooks`/`_virality_score`/`ban_risk` system prompts) is **Russian even for EN niches**, and `ANGLES` (viral angle library, injected into the prompt) is **RU-only text — it leaks Russian into EN prompts for `ai_lifehacks_en`**. Whole architecture is single-payoff short-form (hook/loop/replay), meaningless for 8–12 min chapters.
- (c) For EN Shorts: translate scaffold + add EN `ANGLES`, keep gates and the whole judge/retry machinery (it's format-independent gold). For long-form: **rewrite** the generator (chaptered outline → per-chapter expansion, ~1300–2000 words, retention re-hooks per chapter), but reuse `_parse`, `_clean_line`, gating/feedback-loop pattern, `topics_db` reservation, `ban_risk`.

### pipeline/voice.py (449 loc) — **copy-as-is (minor edits)**
- (a) `synthesize(chunks, voice, rate, workdir, engine, lang, speed) -> (timed_chunks, audio_path, total_dur)`. Engines: edge-tts (WordBoundary word timing), ElevenLabs (with-timestamps alignment, multi-key rotation + persisted cooldown), XTTS/MOSS local venv workers. Concat via ffmpeg, integrity checks (silent/broken chunk → hard fail), then **Groq Whisper word-level re-alignment** (`_groq_align`, lang param already handles "en", model whisper-large-v3-turbo) — ground-truth subtitle timing.
- (b) EN already supported: fallback voices `en-US-AndrewNeural`/`AriaNeural` exist; ElevenLabs voice table is English names. Long-form caveats: ElevenLabs free keys (~10k chars/mo) can't sustain daily 8–12 min (≈9–11k chars/video) → **edge-tts is the realistic long-form engine**; XTTS/MOSS worker timeout 1800s may be tight for 10-min on CPU; Groq audio file limit (~25MB) is fine for 10-min m4a.
- (c) Copy; pick EN voices in niches config; maybe bump worker timeouts.

### pipeline/broll.py (597 loc) — **copy-with-edits (Shorts) / significant edits (16:9 long)**
- (a) `fetch_for(timed_chunks, niche, workdir, mode, character) -> [{path, kind, source, dur, query}]`. Source cascade: Pexels → Pixabay → Coverr → NASA SVS (PD) → Internet Archive (PD/CC-BY license-checked) → reuse → generated gradient. Semantic re-ranking of stock candidates by token overlap; cross-video media dedup via `topics_db.used_media`; AI-image modes (`ai_images`, `ai_video_hook`, `depth_video` via DepthFlow venv); slot planner: 3×1.0s intro pattern-interrupt + variable 2.8–4.4s slots snapped to phrase boundaries.
- (b) Vertical hardcodes: Pexels `orientation=portrait` + portrait file filter (`height >= width`, target 1920-height); gradient generator uses `core.W/H`; NASA filter tuned to ≤1080p. Queries are **already English** (stock is EN-indexed) — no language change needed. Long-form math problem: 10 min ÷ ~3.5s slots ≈ 170 clips/video → Pexels 200 req/h rate limit, ~2–4 GB downloads, 170 ffmpeg normalizations; slot cycle must be rewritten (8–15s slots, chapter-level queries, image+KenBurns heavy mix).
- (c) Shorts EN: change nothing. 16:9: flip orientation params (`orientation=landscape`, `width>=height`, target 1920-width), parametrize gradient size, rework `SLOT_CYCLE`/`INTRO_SLOTS` for long-form.

### pipeline/subtitles.py (212 loc) — **copy-with-edits (Shorts) / skip-or-rewrite (long-form)**
- (a) `build_ass(timed_chunks, out_ass, mode="popin"|"karaoke")`, `accent_times()`. One-word pop-in with YAKE keyword highlight (yellow), or karaoke `\kf` phrases; anti-flicker min durations.
- (b) Hardcodes: ASS header `PlayResX: 1080 / PlayResY: 1920`, `CENTER_X=540, CENTER_Y=1190` (62% of vertical height), font sizes 132/104/82 tuned to 1080-wide vertical, font "Montserrat Black" (fine for Latin; ships in `assets/fonts/`). YAKE supports `en` already; `_LOOP_STOP`/short-word logic includes EN.
- (c) EN Shorts: copy as-is. Long-form 16:9: this burned-in one-word style is wrong; either burn standard 2-line bottom captions (small rewrite of header/geometry) or skip burn-in and upload an SRT/timed text instead (word timings are already available from voice.py — easy SRT emitter).

### pipeline/assemble.py (211 loc) — **copy-with-edits**
- (a) `render(broll_list, full_audio, total_dur, ass_path, out_mp4, music_path, accents, loop) -> mp4`. Normalizes each clip to W×H cover-crop (+Ken Burns on images), concat, seamless loop xfade (replay trick), color grade, burns ASS + top progress bar, voice de-esser/compressor, sidechain-ducked music, loudnorm -14 LUFS, H.264 CRF 21 maxrate 6M +faststart.
- (b) All geometry from `core.W/H/FPS` (so parametrizing core gets you 90% there). Shorts-specific: loop xfade (pointless/harmful in long-form), progress bar, Ken Burns zoom constants tuned to 1.15× canvas. `run_retry` timeout 900s too small for a 10-min final encode on CI (~2–4× realtime with preset medium) — raise to ~3600 and consider `preset veryfast`.
- (c) Copy; add `loop=False`, drop progress bar for long-form, parametrize resolution, raise timeouts.

### pipeline/qa.py (277 loc) — **copy-with-edits**
- (a) `check(video, workdir, niche) -> {ok, issues, technical{}, visual{}, *_unverified}`. Technical: ffprobe duration/resolution, mean_volume fail-**closed** (<-70dB = fail), audio-longer-than-video >0.25s = fail, freezedetect ≥1.5s, scene-cut density + intro-cut warning. Visual: Gemini 2.5 Flash multi-key on 4 extracted frames (deformed hands/faces/AI text), fail-open when Gemini unavailable, with honest `visual_unverified` flags.
- (b) Hardcodes: resolution must equal `core.W×core.H`; duration window 8s–70s ("Shorts ≤60s", `niche.max_seconds` override exists); ffmpeg sub-checks have 120–180s timeouts — a 10-min video needs 3 full decodes and **will blow those timeouts** (fail-open but unverified); 4 frames too few for 10 min.
- (c) Copy; parametrize duration window per format, scale timeouts and frame count by duration.

### pipeline/imagegen.py (233 loc) — **copy-with-edits**
- (a) `generate(query, niche, out, seed, character)` / `generate_raw(prompt, out, seed)`. Cascade NVIDIA FLUX.1-dev (`NVIDIA_API_KEY`, best quality) → Pollinations (nanobanana if `POLLINATIONS_API_KEY`, else flux, keyless) → Gemini image (dead on free tier, limit 0). Daily quota counters (nvidia 40 / pollinations 120), black-frame detection, opaque-key cooldowns.
- (b) 9:16 baked in three places: prompt text "9:16 vertical", NVIDIA `768x1344`, Pollinations `width/height = core.W/H`, Gemini `aspectRatio: 9:16`. Prompts are already English.
- (c) Add an aspect parameter (16:9 → "16:9 widescreen", NVIDIA 1344×768).

### pipeline/parser.py (488 loc) — **copy-as-is** (add config)
- (a) `gather(niche) -> [{title, source, url, ts, weight}]`. Sources: Google News RSS, Reddit top+rising, HackerNews, trendspyg/Google Trends RSS, Google Suggest, Wikimedia pageviews, competitor YouTube RSS (`niche.rss_channels`), Telegram t.me/s, VK search. All sanitized against prompt injection.
- (b) Already lang-aware: `lang=="en"` → News `gl=US/hl=en`, Trends `geo=US`, Suggest `hl=en`, EN keyword lists in `NICHE_TRENDS`; RU-only sources (Wikimedia-ru, Telegram, VK) are auto-skipped for EN niches. Only gap: `NICHE_TRENDS`/subreddits cover 5 categories — new US niches need their keyword/subreddit entries.
- (c) Copy; extend `NICHE_TRENDS` for new niches; optionally add EN-Wikipedia pageviews (one-line: `wiki_top("en.wikipedia")` is already parameterized).

### pipeline/selector.py (125 loc) — **copy-with-edits**
- (a) `pick_topics(niche, n, recent) -> [topics]`: trends → LLM producer picks/reframes N topics, avoids recent (merged with `topics_db.recent_titles`), demonetization filter, injection sanitizing, fallback to raw headlines.
- (b) System prompt scaffold is Russian (output topics come out in EN via `lang_word` instruction, but the meta-prompt is RU).
- (c) Translate one prompt block; otherwise verbatim.

### pipeline/topics_db.py (334 loc) — **copy-as-is**
- (a) SQLite (`DATA_ROOT/factory.db`, WAL) semantic dedup: translit+stem fingerprints, Jaccard+subset similarity, **atomic reserve/commit/release** (BEGIN IMMEDIATE) closing the parallel-build race; `used_media` table for cross-video stock dedup; `recent_titles`.
- (b) None material — stemming/translit handles EN (`_STOP` already contains EN words; `_stem` suffix stripping is RU-oriented but harmless for EN).
- (c) Verbatim. This module + the reserve/commit/release protocol is one of the most valuable pieces.

### pipeline/thumbnail.py (568 loc) — **copy-with-edits / partial rewrite for 16:9**
- (a) `make_best_for_meta(video, meta, out) -> jpg`: N candidates (AI FLUX background or best-of-5 sharpness/brightness-scored video frames) + ALL-CAPS wrapped title with stroke + niche accent color, Gemini-vision picks most clickable + readability QA.
- (b) Canvas = `core.W×core.H` (vertical); text layout percentages tuned to 9:16; AI bg prompt says vertical. Long-form YouTube needs **1280×720 16:9** thumbnails with different composition (face/object right, text left) — that's a layout rewrite, though frame-scoring, wrapping, vision-selection machinery all reuse.
- (c) Keep scoring/selection/QA; rewrite the compose step for 16:9.

### pipeline/autopilot.py (409 loc) — **rewrite, but steal the reliability skeleton**
- (a) `run(output, niche)` where output ∈ ig_vk|text|youtube|tiktok. Routes builds to platform adapters or TG manual-review queue. Reliability layer: per-day idempotency (`state/posted.json`), success ledger (`state/posts.jsonl`), transient-only retry (`_retry_pub`, delays 15/45/120), time budget (`CF_RUN_BUDGET_S` default 4800s = 80 min), per-niche SIGALRM hard cap (`CF_NICHE_CAP_S` 1500s, raised as BaseException to pierce inner except-Exception), round-robin niche cursor persisted between runs, QA-fail → TG critical alert.
- (b) Wired to `panel.db` bundles and RU platforms (VK/IG/Threads); YouTube path goes to a Telegram queue for *manual* upload, not the API.
- (c) New project: rewrite `run()` around direct `youtube.publish`, but copy `already_posted`/`_mark_posted`/`_ledger`/`_retry_pub`/budget/SIGALRM/cursor verbatim — they encode hard-won CI lessons.

### adapters/youtube.py + youtube_auth.py — **EXISTS, WORKS, copy-with-edits**
- State: full YouTube Data API v3 uploader — OAuth installed-app flow (`youtube_auth.py`, one-time local browser run per channel), auto token refresh with write-back, **multi-channel**: per-account token files `DATA_ROOT/yt_tokens/<secret_ref>.json` with fallback to single `YT_TOKEN_FILE` (`~/.config/content-factory/yt_token.json`); resumable upload (1 MiB chunks, `next_chunk(num_retries=5)`), custom thumbnail set (never fails the upload), category map per niche, `privacyStatus: public`, `selfDeclaredMadeForKids: False`. Env: `YT_CLIENT_SECRET_FILE`, `YT_TOKEN_FILE`.
- Documented operational caveats in the file: until the GCP project passes the **YouTube API Compliance Audit, all API uploads silently become private** (insert returns 201); hidden ~7 uploads/day/channel limit — enforced outside the adapter (`YT_DAILY_CAP = 7` in factory.py via history counting).
- Edits for the new project: it **force-appends `#Shorts` to every description** (line 101-102) — must be conditional on format for long-form; `_YT_CAT` map keyed by RU niche ids; long-form additionally wants `snippet.defaultLanguage`, maybe playlist insert. Otherwise drop-in.

### factory.py / niches.json / build.py
- factory.py: CLI (doctor/build/batch/post/run/autopost/v2) with QA gate + posting idempotency at `_post_dir`; YT daily cap check. Pattern-copy.
- niches.json: schema is the contract (`id, lang, engine, voice, rate, speed, tone, topic_brief, broll_hint, palette, hashtags, cta, platforms, broll_mode, subtitle_mode, format, category, rss_channels, has_yt_tiktok, max_seconds`). One EN niche exists (`ai_lifehacks_en`, disabled) proving the lang path was exercised. New file for US niches; schema needs a `format`/`video_kind` field (short|long) and per-format overrides.
- pipeline/build.py: `build_video(niche_id, topic, broll_mode, max_attempts=2, serial, platform)` — full orchestration with regeneration on QA fail, "force-publish best visual-only-defect candidate" policy (tech defects and ban-risk never forced), topic reserve commit/release, media archiving, captions builder. `PLATFORM_SPECS` word targets (66/80/88) are Shorts-only. Captions builder has RU specifics (#Shorts-first tag logic is fine; VK block, RU stop-words in topic-tag, RU financial disclaimer). Verdict: copy-with-edits for Shorts; long-form needs its own build path (chapters, no 62–92 gate) reusing the same skeleton.

## Cross-cutting answers

**Secrets loading.** Local: `core.load_local_secrets()` reads `~/.config/content-factory/secrets.env` then `~/.config/content-engine/secrets.env` (KEY=VALUE lines, `os.environ.setdefault` — never overrides), registers secret-looking values for log masking. CI: single GitHub secret **`SECRETS_ENV`** containing the whole env file, written verbatim by the workflow to `~/.config/content-factory/secrets.env` (`printf '%s' "$SECRETS_ENV" > ...`), then the same loader runs. Plus `MEDIA_PAT` secret exported as `GITHUB_TOKEN` for the media_host adapter (pushes mp4 to a `cf-media` repo for public raw URLs — IG/Threads only, not needed for YouTube). This pattern copies directly.

**GitHub Actions autopilot.** `autopilot.yml`: 4 daily cron slots (08/11/14/17 UTC = 11/14/17/20 MSK), each mapped to an output by matching `github.event.schedule` string; `workflow_dispatch` with output/niche inputs; `concurrency: group autopilot, cancel-in-progress: false`; `timeout-minutes: 120`; ubuntu + python 3.12 + apt ffmpeg + pip requirements. **Budget**: in-process `CF_RUN_BUDGET_S` (default 4800s) stops starting new niches, `CF_NICHE_CAP_S` (1500s) SIGALRM-kills a stuck niche; skipped niches reported to TG and picked up next run via persisted cursor. **State commit-back**: `CF_DATA_ROOT=$workspace/state/cfdata` so history/cooldowns/topics survive ephemeral runners; `if: always()` step commits `state/` (`[skip ci]`) with `git pull --rebase --autostash` + push, 5 retries, hard job failure if push fails. On job failure: TG alert via reporter with run URL. `report.yml`: daily 18:00 UTC token preflight + channel stats report to TG + snapshot commit-back. Whole file structure copies with renamed outputs.

**QA gate.** `qa.check()` after render, result stored in `meta.json["qa"]`. `build_video` retries once on failure; visual-only failures (tech ok, ban-risk not high) → best candidate force-published with `qa.forced=true`; tech/ban failures never forced. Publication paths (`_post_dir` CLI, autopilot) refuse `qa.ok=false` and send a critical TG alert (lost slot). Volume check fail-closed; freeze/scene/visual checks fail-open with explicit `*_unverified` flags. Plus pre-publication LLM `ban_risk` (high → blocks) and idempotency (already-posted platform is skipped).

**Key risks specific to the new project** (not present in current code): long-form encode/QA timeouts (raise `run_retry`/subprocess timeouts), b-roll volume at 8–12 min (rework slot planner; stock API rate limits), ElevenLabs quota can't cover daily long-form (use edge-tts), YouTube API compliance audit prerequisite for public API uploads, `#Shorts` auto-append must become format-conditional, and the RU system-prompt scaffolds in script.py/selector.py should be fully translated rather than relying on the `lang` instruction line.

**Bottom line.** Copy-as-is: `llm.py`+`llm_providers.json`, `topics_db.py`, `parser.py`, `youtube_auth.py`, most of `core.py` and `voice.py`. Copy-with-edits: `broll.py`, `assemble.py`, `qa.py`, `imagegen.py`, `subtitles.py`, `selector.py`, `youtube.py`, `factory.py`, workflows. Rewrite (reusing patterns): `script.py` long-form generator, `build.py` long-form path, `autopilot.py` orchestration, `thumbnail.py` 16:9 composer, `niches.json` content.