All data gathered. Compiling the final report.

# TTS for Long-Form YouTube Narration on Free CI (July 2026)

## Context that shapes the answer

- **GitHub Actions free runners** = 4-core AMD EPYC, no GPU; public repos get unlimited minutes, private repos 2,000 min/mo.
- **YouTube's "inauthentic content" policy** (renamed from "repetitious content", enforced since July 15, 2025): AI narration is allowed and monetizable, but "low-quality, robotic TTS is a major red flag for reviewers"; near-human voices pass review. "Altered content" disclosure checkbox required. ([fluxnote guide](https://fluxnote.io/guides/using-ai-voices-for-youtube-monetization), [Typecast on the July 15 update](https://typecast.ai/learn/youtube-ai-monetization-july-15-ypp-update/))
- 10 min of narration ≈ 9,000 chars; 300–400 min/mo ≈ ~360k chars.

## Comparison table

| Engine | US-EN naturalness | License (commercial?) | CPU: time for 10 min audio (4-core CI) | CI install | Cost /400 min/mo |
|---|---|---|---|---|---|
| **Kokoro-82M v1.0** | Very good — Arena Elo 1056, most natural free local option; beats models trained on 1M+ hrs | **Apache 2.0 ✓** | **~5 min** (RTF 0.45–0.51 measured on 4-core EPYC, ONNX) | Easy: `pip install kokoro-onnx` + espeak-ng, ~80–330 MB model | **$0** |
| **Chatterbox / Turbo (350M, Dec 2025)** | Best open-weight: 63.75% preferred over ElevenLabs (vendor blind test); paralinguistic tags | **MIT ✓** (PerTh watermark built in) | Slower than realtime; est. 30–90 min, needs chunking; fits 6h job limit | Medium: PyTorch CPU wheel ~800 MB+ | $0 (heavy compute) |
| **edge-tts 7.2.8** | Good — Azure neural voices (Andrew/Brian etc.), widely used on monetized channels | Gray zone: violates MS ToS; no takedowns in 3 yrs, rate-limits possible | ~1–2 min (network-bound) | Trivial: `pip install edge-tts` | $0 |
| **Piper (piper1-gpl v1.4.2)** | Mediocre — clearly synthetic; flagged-as-robotic risk | GPL-3.0 (outputs fine) | ~1 min (10× realtime) | Easy | $0 |
| **Supertonic 3 (99M)** | Below Kokoro — "lacks warmth and natural prosody" at usable 5-step | OpenRAIL-M (some restrictions) | ~3 min (RTF 0.31) | Easy (ONNX) | $0 |
| **MeloTTS-English-v3** | Below Kokoro; fine for UI, weak for retention | MIT ✓ | ~10 min (≈realtime) | Easy | $0 |
| **StyleTTS2** | Arena Elo 879 — superseded by Kokoro (same lineage) | MIT ✓ | ≈realtime | Painful (manual setup) | $0 |
| **XTTS v2 (Coqui/Idiap)** | Elo 886 | **CPML — non-commercial ✗** (Coqui dead, no license to buy) | Very slow on CPU | — | ✗ disqualified |
| **F5-TTS** | Good quality but | **CC-BY-NC-4.0 ✗** (even after finetune); OpenF5 (Apache) is worse | 100–300 min (10–30× slower than realtime) | — | ✗ disqualified |
| **OpenAudio S1-mini (Fish)** | Excellent (S2 Pro is #1 open-weight, Elo 1129) | **CC-BY-NC-SA ✗** for local weights (authors say YT-with-credit OK — risky for monetized) | Needs GPU | — | ✗ disqualified |
| **VibeVoice-1.5B (MS)** | Near-frontier | MIT ✓ | GPU-only in practice (7 GB VRAM) | — | ✗ on CPU CI |
| **OpenAI gpt-4o-mini-tts** | Very good, steerable tone via instructions | API, commercial ✓ | ~1 min (API) | Trivial | **~$6** ($0.015/min) |
| **OpenAI tts-1-hd** | Very good | API ✓ | ~1 min | Trivial | ~$11 ($30/1M chars) |
| **ElevenLabs** | Best-in-class (v3 Elo 1178) | API ✓ | ~1 min | Trivial | ✗ Creator $22 = only 100–200 min |
| **Groq TTS (PlayAI Dialog)** | Good, EN/AR only | API ✓ | ~1 min (10× realtime) | Trivial | ~$18 ($50/1M chars) — just over |
| **PlayHT** | Very good | API ✓ | ~1 min | Trivial | ✗ Creator $39 |
| **Unreal Speech** | Decent, below ElevenLabs | API ✓ | ~1 min | Trivial | free 250k chars (~270 min), then $16/1M |

Notes: no newer/larger English Kokoro exists — v1.0 (Jan 2025) is still current, "v2.3.1" refers to the CLI wrapper. Kitten TTS (25 MB) and NeuTTS Air run on CPU but aren't narration-retention grade.

## Recommendations

**$0 pick: Kokoro-82M (ONNX)** — the only engine combining Apache-2.0 commercial license, genuinely natural prosody (passes YouTube's "robotic TTS" bar; Elo within ~120 of ElevenLabs), and a measured RTF ~0.5 on exactly the 4-core EPYC hardware GH Actions provides: a 10-min video renders in ~5 min, 40 videos/mo ≈ 240 CI minutes. Voices `af_heart`/`af_bella`/`am_michael` for US narration. Keep edge-tts as a zero-compute fallback (better in a pinch, but ToS-gray and can break without notice — don't build the business on it). If maximum quality at $0 matters more than CI time, Chatterbox Turbo (MIT) is the quality ceiling, at 30–90 min render per video and mandatory text chunking.

**≤$15/mo pick: OpenAI gpt-4o-mini-tts** — ~$6/mo for 400 min, near-ElevenLabs naturalness, steerable ("calm documentary narrator") via the instructions param, one `pip install openai` in CI, no compute. ElevenLabs quality is unreachable at this budget (100 min for $22); Groq/PlayAI lands at ~$18. Practical hybrid: Kokoro by default + gpt-4o-mini-tts for flagship videos.

**Sources:** [Kokoro-82M (HF)](https://huggingface.co/hexgrad/Kokoro-82M) · [Kokoro vs Supertonic 3 CPU benchmark](https://heyneo.com/blog/kokoro-tts-vs-supertonic-3-tts) · [Kokoro ONNX 4-core benchmark gist](https://gist.github.com/efemaer/23d9a3b949b751dde315192b4dcf0653) · [TTS Arena leaderboard 2026](https://offlinetts.com/blog/tts-arena-leaderboard-2026/) · [Chatterbox (Resemble, MIT)](https://www.resemble.ai/learn/models/chatterbox) · [Chatterbox Turbo](https://huggingface.co/ResembleAI/chatterbox-turbo) · [OpenAudio S1-mini license discussion](https://github.com/fishaudio/fish-speech/discussions/1001) · [XTTS/CPML status](https://localaimaster.com/blog/xtts-coqui-commercial-license) · [F5-TTS license](https://www.promptquorum.com/power-local-llm/local-tts-voice-cloning-piper-coqui-xtts) · [edge-tts](https://github.com/rany2/edge-tts) · [Piper1-GPL](https://localaimaster.com/blog/piper-tts-setup-guide) · [Supertonic](https://github.com/supertone-inc/supertonic) · [VibeVoice](https://huggingface.co/microsoft/VibeVoice-1.5B) · [MeloTTS](https://github.com/myshell-ai/MeloTTS) · [gpt-4o-mini-tts pricing](https://texttolab.com/blog/openai-tts-pricing) · [ElevenLabs pricing](https://elevenlabs.io/pricing) · [Groq PlayAI TTS](https://console.groq.com/docs/text-to-speech) · [Unreal Speech pricing](https://unrealspeech.com/pricing) · [PlayHT pricing](https://voice.ai/hub/tts/play-ht-pricing/) · [YouTube AI voice policy 2026](https://fluxnote.io/guides/using-ai-voices-for-youtube-monetization)