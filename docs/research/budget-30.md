# Research: Best $30/mo for a Free Faceless YouTube Pipeline (July 2026)

## TL;DR — Recommended allocation

**Key finding: Modal's Starter plan is $0 and includes $30/month of free compute credits — that's an entire second budget you're not using.** So the optimal play is stacked:

| Spend | What | Gets you |
|---|---|---|
| **$0** | Modal Starter free credits (~$30/mo compute) | ~60–120 free Wan 2.2 i2v clips/mo (self-hosted, high setup effort) |
| **$6** | OpenAI `gpt-4o-mini-tts` (~$0.015/min) | All 400 min/mo of narration — the single biggest retention upgrade over CPU TTS |
| **$24** | fal.ai pay-per-use i2v (LTX-2 Fast $0.04/s @1080p, Wan 2.2 5B $0.15/clip) | ~100–160 additional 5s B-roll clips/mo, zero setup |
| **$0** | Stock, GitHub Actions | Stay free (see below) |

If Modal setup effort isn't worth it to you, the $6 TTS + $24 fal.ai split alone is the answer.

---

## 1. Modal.com — YES, and it's free money

- Starter plan: **$0/mo with $30/mo free credits**, 10 concurrent GPUs, 100 containers ([modal.com/pricing](https://modal.com/pricing), verified directly).
- GPU rates: H100 $0.001097/s (~$3.95/hr), L40S $0.000542/s (~$1.95/hr), A100-80GB $0.000694/s.
- Wan 2.2 14B, 5s clip: ~60s–3 min on H100 with an optimized stack ([Baseten benchmark](https://www.baseten.co/blog/wan-2-2-video-generation-in-less-than-60-seconds/)); real-world reference cost $0.17–0.21 (480p) / $0.42–0.50 (720p) per 5s clip on a $2.50/hr H100 ([Spheron](https://www.spheron.network/blog/deploy-wan-2-1-ai-video-generation-gpu-setup/)). On Modal expect **~$0.25–0.50/clip 720p → 60–120 clips per $30 credits**.
- Setup effort: **high** (Modal app + ComfyUI/diffusers, ~27GB weights in a Volume, GPU memory snapshots get cold starts to seconds — [example](https://tolgaoguz.dev/post/comfy-workflow-api-with-modal/), [modal-comfyui repo](https://github.com/caru-ini/modal-comfyui)). One-time ~1–2 days of work, then it's an API in your Actions workflow.

## 2. Paid TTS — highest quality-per-dollar of anything on this list

Voice covers 100% of watch time; robotic CPU TTS is the #1 retention killer in documentary faceless content. For 300–400 min/mo:

| Option | Cost for 400 min | Verdict |
|---|---|---|
| **OpenAI gpt-4o-mini-tts** | **~$6** ($0.60/1M text + $12/1M audio tokens ≈ $0.015/min) | **Winner** — steerable via instructions param, 13 voices ([pricing](https://developers.openai.com/api/docs/pricing), [analysis](https://tokenmix.ai/blog/gpt-4o-mini-tts-cheapest-tts-api-2026)) |
| MiniMax speech-2.5-turbo | ~$14 ($0.04/1k chars direct) | Near-ElevenLabs quality, good runner-up ([MiniMax pricing](https://platform.minimax.io/docs/pricing/overview)) |
| ElevenLabs Creator $22 | only ~100 min Multilingual / ~200 min Flash — **short of 400 min** | Best raw quality; viable only as "hero voice" for Shorts/intros ([elevenlabs.io/pricing](https://elevenlabs.io/pricing)) |
| ElevenLabs Pro (covers 400 min) | $99 | Over budget |

## 3. fal.ai / Replicate / Runware pay-per-use

- **fal LTX-2 Fast i2v: $0.04/s at 1080p → 5s = $0.20** (~150 clips/$30), 30x faster rendering, native 1080p+audio ([fal LTX-2 fast](https://fal.ai/models/fal-ai/ltx-2/image-to-video/fast)).
- **fal Wan 2.2 5B: $0.15/clip** 5s 720p (~200 clips/$30) ([fal Wan 2.2 5B](https://fal.ai/models/fal-ai/wan/v2.2-5b/image-to-video)); Wan 2.2 A14B $0.10/s → $0.50/clip (~60 clips).
- **Runware: Seedance 1.0 Lite $0.14/clip**, claims cheapest infra for open models ([Runware blog](https://runware.ai/blog/lowest-cost-ai-video-generation-now-on-runware)).
- Zero setup (REST call from Actions) — the pragmatic choice vs Modal.

## 4. Storyblocks — NO

$30/mo (Unlimited All Access) eats the entire budget, and the **Individual license is not perpetual for new use**: published work stays covered, but after cancelling you can't publish new videos with previously downloaded assets ([Storyblocks help center](https://help.storyblocks.com/en/collections/2061820-subscriptions-and-licensing-frequently-asked-questions), [pricing](https://www.storyblocks.com/pricing)). Free Pexels/Pixabay/NASA/Archive.org + FLUX images + AI motion clips beat it at this budget.

## 5. GitHub Actions paid minutes — NO

Standard runners on public repos remain **free and unlimited** in 2026 (prices on paid runners even dropped up to 39% in Jan 2026) ([GitHub docs](https://docs.github.com/en/actions/concepts/billing-and-usage), [changelog](https://github.blog/changelog/2025-12-16-coming-soon-simpler-pricing-and-a-better-experience-for-github-actions/)). Paying only buys faster ffmpeg renders (larger runners) — zero output-quality impact. Worst quality-per-dollar option here.

---

## Ranking by retention impact per dollar

**(a) 8–12 min documentary:**
1. TTS upgrade ($6, OpenAI) — affects every second of every video
2. Modal free credits for Wan i2v B-roll ($0 cash) — motion beats static Ken Burns
3. fal.ai LTX-2/Wan top-up ($0.15–0.20/clip) — animate the 10–15 key beats per video
4. Storyblocks — marginal over free stock
5. GHA minutes — zero quality impact

**(b) Vertical Shorts:**
1. i2v clips (fal LTX-2 Fast / Modal credits) — a 30–60s Short can be 100% AI motion for $1.20–2.40; motion density is the Shorts retention lever
2. TTS upgrade — still matters, but voice time per Short is small ($6 covers both formats anyway)
3–5. Same order as above.

Not evaluated but noticed: fal also lists newer Wan 2.6 and LTX-2.3 endpoints — worth re-checking prices at implementation time, as per-clip costs have been trending down quarterly.

**Sources:** [Modal pricing](https://modal.com/pricing) · [Baseten Wan 2.2 benchmark](https://www.baseten.co/blog/wan-2-2-video-generation-in-less-than-60-seconds/) · [Spheron Wan GPU costs](https://www.spheron.network/blog/deploy-wan-2-1-ai-video-generation-gpu-setup/) · [modal-comfyui](https://github.com/caru-ini/modal-comfyui) · [ComfyUI cold starts on Modal](https://tolgaoguz.dev/post/comfy-workflow-api-with-modal/) · [OpenAI API pricing](https://developers.openai.com/api/docs/pricing) · [gpt-4o-mini-tts cost analysis](https://tokenmix.ai/blog/gpt-4o-mini-tts-cheapest-tts-api-2026) · [ElevenLabs pricing](https://elevenlabs.io/pricing) · [MiniMax pricing](https://platform.minimax.io/docs/pricing/overview) · [fal LTX-2 Fast i2v](https://fal.ai/models/fal-ai/ltx-2/image-to-video/fast) · [fal Wan 2.2 5B](https://fal.ai/models/fal-ai/wan/v2.2-5b/image-to-video) · [Runware video launch](https://runware.ai/blog/lowest-cost-ai-video-generation-now-on-runware) · [Storyblocks pricing](https://www.storyblocks.com/pricing) · [Storyblocks licensing FAQ](https://help.storyblocks.com/en/collections/2061820-subscriptions-and-licensing-frequently-asked-questions) · [GitHub Actions billing](https://docs.github.com/en/actions/concepts/billing-and-usage) · [GitHub Actions pricing update](https://github.blog/changelog/2025-12-16-coming-soon-simpler-pricing-and-a-better-experience-for-github-actions/)