Catalog found and read. All relevant entries extracted below.

**Source files (all read):** `/home/baronpavel/Documents/Tools_Research_2026-06-05/КАТАЛОГ_по_задачам.md` (index), `ПОДРОБНО_ПО_ИНСТРУМЕНТАМ.md`, `СПРАВОЧНИК_полный.md` (355 tools, sections: AI Video 12 шт., AI Image 11 шт., Voice/TTS/STT 11 шт., Stock media 19 шт., Workflow automation 18 шт., Free LLM tiers 12 шт.), `ОЧЕРЕДЬ_УСТАНОВКИ.md` (install status).

# Tools from the catalog relevant to an automated YouTube video factory

## Video rendering / editing
- **Remotion** — programmatic video from React/TS components (data → video). License free for individuals/companies ≤3 people, commercial OK; 4+ people needs Company License ($100+/mo). Already installed with a `landing-case` template (`remotion-video` skill exists). Core renderer for templated videos.
- **FFmpeg** — encode/trim/concat/scale/overlay/audio-mux CLI. Fully free (LGPL/GPL). The glue: stitch clips into 1080×1920 or 1920×1080, burn captions, mix music, batch-export. Pairs with whisper for auto-subs.
- **ComfyUI** — node-based diffusion workflow engine, de-facto frontend for all open video/image models; HTTP API scriptable from Python. Free (GPL-3.0) but needs GPU. Single orchestrator for AI-video + ffmpeg post-processing.

## AI video generation (all open-weights, need NVIDIA/cloud GPU unless noted)
- **Wan2.2 (Alibaba)** — text/image-to-video; light TI2V-5B does 720p@24fps on one RTX 4090. Apache-2.0, LoRA-trainable. Best free image-to-video for b-roll from stills.
- **LTX-Video / LTX-2 (Lightricks)** — real-time DiT video; LTX-2 makes synced audio+video up to 4K/50fps on consumer GPUs. Open-weights (not OSI — read terms). Fastest/cheapest to self-host for shorts.
- **HunyuanVideo 1.5 (Tencent)** — 13B-class text-to-video; v1.5 is Apache-2.0 (commercial-safe; original 13B excludes EU/UK/KR). Highest quality open T2V.
- **CogVideoX (THUDM)** — T2V/I2V 2B/5B with first-class Diffusers support; 2B Apache-2.0, 5B custom license.
- **Mochi (Genmo)** — 10B Apache-2.0 text-to-video, good abstract/b-roll; high VRAM.
- **AnimateDiff** — SD-based animation, Apache-2.0, stale but alive via ComfyUI nodes; short clips only.
- **SadTalker** — photo + TTS audio → talking-head presenter video, free. Faceless-channel "spokesperson" without avatar SaaS.
- **Pollinations.ai** — keyless free HTTP API for image/text/audio/video generation (no signup, no GPU). Rate-limited/best-effort, commercial rights depend on model chosen; register free referrer/token for production. The zero-infra fallback.
- **HF Spaces (video-generation)** — free browser demos of Wan/LTX/CogVideoX without local GPU; queue-limited, for testing.

## Text-to-speech (narration)
- **Kokoro-82M** — Apache-2.0, 82M-param TTS, runs fast on CPU (no GPU bill), 9 languages. ⚠️ No Russian (EN/ES/FR/HI/IT/JP/PT/ZH). Companion **Kokoro-FastAPI** (Docker) exposes OpenAI-compatible `/v1/audio/speech`. Best free CPU narration for EN channels.
- **Chatterbox (Resemble AI)** — MIT, SOTA open TTS, zero-shot voice cloning, emotion tags ([laugh] etc.), 23+ languages incl. Russian (quality varies). GPU recommended. Premium narration, commercially safe.
- **Coqui XTTS-v2** — voice cloning from a few seconds of audio, 16 langs incl. Russian (strongest free RU cloning). Archived — use idiap fork (`pip install coqui-tts`); ⚠️ model weights non-commercial-leaning license.
- **Piper** — near-instant CPU TTS with prebuilt Russian voices, MIT/GPL. Synthetic-sounding, no cloning; good for high-volume/low-cost.
- **OpenVoice (MyShell)** — MIT voice cloning, fully commercial-safe; V2 langs EN/ES/FR/ZH/JP/KO (no RU).
- **F5-TTS** — top-tier expressive cloning; MIT code but official weights CC-BY-NC (non-commercial) — community fine-tunes add RU.
- **Silero** — best plug-and-play Russian voices (aidar/baya/kseniya/xenia); ⚠️ CC-BY-NC — commercial use needs paid license.
- **Dia (nari-labs)** — Apache-2.0, two-speaker dialogue TTS with nonverbals — podcast/skit-format videos. English only, GPU needed.

## Subtitles / captions (STT)
- **faster-whisper** — MIT, Whisper 4x faster, incl. Russian; auto-caption generation (SRT) locally, CPU works, GPU better.
- **whisper.cpp** — MIT, dependency-free C++ Whisper, single binary, runs on cheap VPS/CPU. For caption generation inside lightweight CI runners.

## Stock footage / images APIs
- **Pixabay API** — 5.7M+ photos/videos, no attribution, commercial OK. Free key; **100 req/60s**, must cache 24h, no hotlinking/mass downloads. Safest b-roll source (no credit clutter). ⚠️ Music/SFX not in the API.
- **Pexels API** — ~3M curated photos + 4K vertical/horizontal videos, commercial OK (credit requested, loosely enforced). **~200 req/hr, 20k/mo** (raisable free on request). Best aesthetic. Also **pexels-mcp-server** for agent-driven search.
- **Unsplash API** — top editorial photos (no video). Demo tier **50 req/hr**, production 5000 req/hr after approval; attribution + download-trigger endpoint mandatory. Also unsplash-mcp-server.
- **Coverr** — free stock video + background music with real Content API (demo 50 calls/hr, prod 2000/hr). ⚠️ Attribution required on free tier; forbids AI-training use.
- **Lorem Picsum** — placeholder images, prototyping only (no per-image license).
- **free-stock-images-mcp** — one MCP over Unsplash/Pexels/Pixabay/Freepik/Burst/StockVault.

## AI image generation (thumbnails, backgrounds)
- **FLUX.1 [schnell]** — Apache-2.0, commercial-safe 12B text-to-image (⚠️ FLUX.1-dev is NON-commercial). ~24GB VRAM ideal, NF4/GGUF quants on 8-12GB. Note from memory: user already has NVIDIA FLUX free API key as first-choice generator.
- **Qwen-Image / Qwen-Image-Edit** — Apache-2.0, best-in-class *text rendering inside images* (incl. CJK/Cyrillic) → the strongest free option for **thumbnails with legible headline text**; Edit variant for inpaint tweaks; Lightning distill for speed.
- **Pollinations.ai** — keyless URL-based image API (Flux/Turbo models), no GPU/no key; rate-limited shared service. Zero-infra thumbnail fallback.
- **HF Diffusers** — Apache-2.0 Python lib to embed generation directly in the pipeline script.
- **SD-WebUI-Forge** (Flux on 8-12GB VRAM, has API), **Fooocus** (one-click SDXL, 4GB VRAM, LTS-only), **InvokeAI** (inpaint/outpaint canvas) — GUI alternatives.
- **rembg** — MIT, one-line background removal locally (thumbnail subject cutouts).
- **Real-ESRGAN** — free 4x upscaler; ncnn binary works without GPU (upscale AI thumbnails to 1280×720+).
- **IOPaint** — erase watermarks/objects from stock frames; LaMa mode needs no GPU.
- Existing local: **image-gen skill** (local SD via OpenVINO), **fal.ai MCP** (key already set up).

## Music / SFX
- **Freesound API v2** — 600k+ CC sounds (SFX/ambience/loops); free key; filter `license:"Creative Commons 0"` for attribution-free commercial use. Deepest free SFX library. Also **freesound-mcp-server** for agent search with license metadata inline.
- **Coverr** — background music via its Content API (see limits above; attribution on free tier).
- ⚠️ Jamendo noted in catalog as **non-commercial-free only** — avoid. Pixabay music is browse-only, not in API. This is the thinnest category in the catalog — background-music sourcing needs supplementing (e.g., YouTube Audio Library manually).

## Workflow automation / cron / scheduling
- **GitHub Actions cron** — free scheduled runner; **unlimited minutes on public repos** (~2000 min/mo private), max 6h/job, cron auto-disables after 60 days of repo inactivity, triggers can lag minutes. The user's existing content-factory already runs on it — proven pattern.
- **n8n (self-hosted)** — visual automation, 400+ integrations incl. AI nodes; fair-code license (can't resell as SaaS; own workflows OK). Needs Docker/always-on host (currently deferred per ОЧЕРЕДЬ). **n8n-mcp** already installed — Claude can author valid workflows.
- **Prefect / Windmill / Airflow / Inngest / Trigger.dev** — code-first schedulers with retries/backfills/observability; Prefect (Apache-2.0) called out as best fit for the Python stack; Airflow marked overkill.
- **mcp-cron** — MCP server scheduling shell/HTTP/AI-prompt jobs via cron expressions from inside Claude.
- **Healthchecks.io** — dead-man's-switch for cron: free hosted tier **20 checks**; `curl` ping at end of each pipeline run → Telegram alert when a scheduled render silently dies.
- **Uptime Kuma** — self-host uptime/push monitoring, 90+ notification channels; needs always-on host.
- **Upstash Redis** — serverless Redis, REST API; free **256MB + 500k commands/mo + 10 DBs** (already provisioned: DB `adequate-basilisk-79170`). Dedup of published topics, rate-limit counters, queue state.

## LLM providers with free tiers (scripts, titles, descriptions, tags)
- **Groq** — fastest free inference (already in use); **~30 RPM / 6k TPM / 14.4k req/day org-level** — user already has a throttler for 429s.
- **Google Gemini API** — free Flash with 1M context, multimodal, no card; **~1500 req/day**. ⚠️ 2.0 Flash deprecated Jun-2026 (use 2.5/3 Flash); free prompts may train models. Multimodal = can critique generated thumbnails.
- **Cerebras Inference** — 2000+ tok/s, **~1M tokens/day free**, no card; ⚠️ 8192-token context cap, ~30 RPM. Great Groq fallback for script generation.
- **OpenRouter** — one key, ~25 `:free` models; **~20 RPM, 50 req/day** (1000/day after buying $10 credits); may log prompts.
- **Mistral La Plateforme** — free Experiment tier (phone verify), incl. Codestral.
- **Together AI** — ~$5 signup credit + some truly-free endpoints (Llama 3.3 70B Free).
- **Ollama / llama.cpp / vLLM / LM Studio / Jan** — local inference, zero API cost, hardware-dependent.
- **Instructor / Pydantic-AI** — typed JSON output from any of the above (structured script/metadata generation that never breaks the pipeline).

## YouTube upload / scheduling / analytics — GAP in the catalog
The catalog has **no dedicated YouTube Data API, yt-dlp, upload, or analytics tool**. Only tangential mentions: a Claude-skills marketplace whose ~53 skills include "media/automation (audio, video, YouTube...)" (СПРАВОЧНИК line 1546, no specifics), and MarkItDown (converts YouTube URLs → Markdown, useful for competitor-content research, not upload). Thumbnail-specific generators are also absent as a named category — covered above via Qwen-Image/FLUX. Note: per user memory, a working YouTube Shorts autoposting pipeline already exists in `~/projects/content-factory` (Groq → edge-tts → stock → ffmpeg → autopost via GitHub Actions) — edge-tts, notably, is used there but is not in this catalog either. YouTube upload/scheduling/analytics tooling must come from outside the catalog (YouTube Data API v3 free quota: 10k units/day — an upload costs 1600 units ≈ 6 uploads/day/project).