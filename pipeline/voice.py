"""Озвучка кусков. Движки:
  • kokoro — Kokoro-82M через kokoro-onnx (ДЕФОЛТ фабрики: локально, бесплатно, US-EN голоса,
             24 кГц wav; модель ~310МБ автокачается в CACHE_DIR/kokoro при первом вызове).
  • edge   — edge-tts (быстро, есть пословный тайминг через WordBoundary) — фолбэк.
  • elevenlabs — облако с каскадом ключей (посимвольный alignment).
  • xtts/moss  — Coqui XTTS v2 / MOSS-TTS через изолированные venv (опциональны, не обязательны).

Все пути дают одинаковую структуру timed-кусков (audio, dur, start, end, words[]),
дальше общий код склеивает дорожку. Точный пословный тайминг в проде даёт _groq_align
по готовому аудио; собственные тайминги движков — лишь фолбэк.
"""
import asyncio
import json
import os
import time
import base64
import hashlib
import pathlib

import edge_tts
import requests

import sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import core  # noqa: E402

TICKS = 1e7  # 100-нс тики edge-tts → секунды


# ──────────────────────────── edge-tts ────────────────────────────

async def _synth_one(text: str, voice: str, rate: str, out_mp3: pathlib.Path, retries: int = 4) -> list[dict]:
    # boundary="WordBoundary" обязателен: по умолчанию edge-tts отдаёт только SentenceBoundary.
    # Либа сама ретраит DRM-403; остальное (нет аудио, обрыв сети) ретраим тут с бэкоффом.
    last_err = None
    for attempt in range(retries):
        words: list[dict] = []
        try:
            comm = edge_tts.Communicate(text, voice, rate=rate, boundary="WordBoundary")
            with out_mp3.open("wb") as f:
                async for ch in comm.stream():
                    if ch["type"] == "audio":
                        f.write(ch["data"])
                    elif ch["type"] == "WordBoundary":
                        words.append({"w": ch["text"], "off": ch["offset"] / TICKS, "dur": ch["duration"] / TICKS})
            if out_mp3.exists() and out_mp3.stat().st_size > 0:
                return words
            last_err = "пустой аудиофайл"
        except Exception as e:  # noqa: BLE001
            last_err = e
        await asyncio.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"edge-tts не озвучил кусок после {retries} попыток: {last_err}")


def _fallback_words(text: str, dur: float) -> list[dict]:
    """Распределить длительность пропорционально длине слов (для xtts и голосов без тайминга)."""
    toks = [w for w in text.split() if w]
    if not toks:
        return []
    weights = [len(w) + 1 for w in toks]
    total = sum(weights)
    out, cursor = [], 0.0
    for w, wt in zip(toks, weights):
        span = dur * wt / total
        out.append({"w": w, "off": cursor, "dur": span})
        cursor += span
    return out


def _edge_timed(chunks: list[dict], voice: str, rate: str, workdir: pathlib.Path) -> list[dict]:
    timed: list[dict] = []
    cursor = 0.0

    async def _run():
        nonlocal cursor
        for i, ch in enumerate(chunks):
            mp3 = workdir / f"voice_{i:02d}.mp3"
            words = await _synth_one(ch["text"], voice, rate, mp3)
            dur = core.media_duration(mp3)
            if dur <= 0:
                dur = (words[-1]["off"] + words[-1]["dur"]) if words else max(1.0, len(ch["text"]) / 14)
            if not words:
                words = _fallback_words(ch["text"], dur)
            abs_words = [{"w": w["w"], "start": cursor + w["off"], "end": cursor + w["off"] + w["dur"]} for w in words]
            timed.append({**ch, "audio": str(mp3), "dur": dur, "start": cursor, "end": cursor + dur, "words": abs_words})
            cursor += dur

    asyncio.run(_run())
    return timed


# ──────────────────────────── Kokoro (kokoro-onnx, дефолт фабрики) ────────────────────────────

# Официальные файлы модели из релиза thewh1teagle/kokoro-onnx (Apache-2.0, Kokoro-82M).
_KOKORO_BASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
_KOKORO_FILES = {  # имя → минимальный размер (защита от битой закачки/HTML-страницы ошибки)
    "kokoro-v1.0.onnx": 100_000_000,   # реально ~310МБ
    "voices-v1.0.bin": 5_000_000,      # реально ~27МБ
}
# US-EN голоса Kokoro: af_* — женские, am_* — мужские. Передан неизвестный — оставляем как есть,
# kokoro сам бросит понятную ошибку, а synthesize откатится на edge.
KOKORO_DEFAULT_VOICE = "am_michael"
_KOKORO = None  # ленивый синглтон: модель грузится в память один раз за процесс (~секунды)


def _kokoro_ensure_models() -> pathlib.Path:
    """Автоскачивание модели в CACHE_DIR/kokoro при первом использовании (headless, чистый pip)."""
    d = core.CACHE_DIR / "kokoro"
    d.mkdir(parents=True, exist_ok=True)
    for name, min_bytes in _KOKORO_FILES.items():
        dest = d / name
        if dest.exists() and dest.stat().st_size >= min_bytes:
            continue
        core.log(f"kokoro: качаю {name} → {dest}", level="info")
        # модель большая — щедрый таймаут; http_download сам ретраит с бэкоффом
        if not core.http_download(_KOKORO_BASE + name, dest, timeout=1800, retries=3, min_bytes=min_bytes):
            raise RuntimeError(f"kokoro: не скачался {name}")
    return d


def _kokoro_get():
    """Ленивая инициализация kokoro-onnx (фонемизатор espeak-ng приезжает pip-пакетом espeakng-loader)."""
    global _KOKORO
    if _KOKORO is None:
        from kokoro_onnx import Kokoro
        d = _kokoro_ensure_models()
        _KOKORO = Kokoro(str(d / "kokoro-v1.0.onnx"), str(d / "voices-v1.0.bin"))
    return _KOKORO


def _write_wav_s16(dest: pathlib.Path, samples, sample_rate: int) -> None:
    """float32 [-1..1] → mono 16-бит wav (stdlib wave, без новых зависимостей вроде soundfile)."""
    import wave
    import numpy as np
    pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).astype(np.int16)
    with wave.open(str(dest), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm.tobytes())


def _kokoro_timed(chunks: list[dict], voice: str, workdir: pathlib.Path,
                  speed: float = 1.0) -> list[dict]:
    """Синтез по кускам: text → 24кГц wav-файл на кусок (тот же контракт, что edge/xtts).
    Пословный тайминг — пропорциональный фолбэк; в проде его перекроет _groq_align."""
    kokoro = _kokoro_get()
    voice = voice or KOKORO_DEFAULT_VOICE
    spd = min(2.0, max(0.5, speed or 1.0))  # диапазон kokoro.create
    timed: list[dict] = []
    cursor = 0.0
    for i, ch in enumerate(chunks):
        wav = workdir / f"voice_{i:02d}.wav"
        samples, sr = kokoro.create(ch["text"], voice=voice, speed=spd, lang="en-us")
        if samples is None or len(samples) == 0:
            raise RuntimeError(f"kokoro: пустой синтез куска #{i}")
        _write_wav_s16(wav, samples, sr)
        dur = core.media_duration(wav)
        if dur <= 0:
            dur = max(1.0, len(ch["text"]) / 14)
        words = _fallback_words(ch["text"], dur)
        abs_words = [{"w": w["w"], "start": cursor + w["off"], "end": cursor + w["off"] + w["dur"]} for w in words]
        timed.append({**ch, "audio": str(wav), "dur": dur, "start": cursor, "end": cursor + dur, "words": abs_words})
        cursor += dur
    return timed


# ──────────────────────────── XTTS (через venv) ────────────────────────────

def _atempo_chain(speed: float) -> str:
    """atempo поддерживает 0.5–2.0 за один проход; для больших значений — цепочка."""
    parts, s = [], speed
    while s > 2.0:
        parts.append("atempo=2.0")
        s /= 2.0
    while s < 0.5:
        parts.append("atempo=0.5")
        s /= 0.5
    parts.append(f"atempo={s:.3f}")
    return ",".join(parts)


def _xtts_timed(chunks: list[dict], voice: str, lang: str, workdir: pathlib.Path,
                speed: float = 1.0) -> list[dict]:
    if not core.XTTS_VENV_PY.exists():
        raise RuntimeError(f"Нет XTTS venv ({core.XTTS_VENV_PY}). Установи: bash setup_xtts.sh")
    inp = workdir / "xtts_in.json"
    outp = workdir / "xtts_out.json"
    inp.write_text(json.dumps({
        "speaker": voice, "lang": lang, "workdir": str(workdir),
        "chunks": [{"text": c["text"]} for c in chunks],
    }, ensure_ascii=False), encoding="utf-8")
    # XTTS на CPU медленный, а лонгформ — до ~1900 слов (~12 мин аудио) — даём щедрый таймаут
    core.run([str(core.XTTS_VENV_PY), str(core.XTTS_WORKER), str(inp), str(outp)], timeout=3600)
    data = json.loads(outp.read_text(encoding="utf-8"))
    if data.get("error"):
        raise RuntimeError(f"XTTS воркер: {data['error']}")
    res = data["chunks"]
    if len(res) != len(chunks):  # иначе zip молча обрежет → рассинхрон озвучки
        raise RuntimeError(f"XTTS вернул {len(res)} аудио из {len(chunks)} кусков")

    timed: list[dict] = []
    cursor = 0.0
    for i, (ch, pc) in enumerate(zip(chunks, res)):
        audio = pc["audio"]
        # ускорение речи без изменения высоты голоса (atempo)
        if speed and abs(speed - 1.0) > 0.01:
            sped = workdir / f"voice_{i:02d}_spd.wav"
            core.run(["ffmpeg", "-y", "-i", audio, "-filter:a", _atempo_chain(speed), str(sped)])
            audio = str(sped)
        dur = core.media_duration(audio)
        if dur <= 0:  # битый/пустой wav — оценим по длине текста, не роняя таймлайн
            dur = max(1.0, len(ch["text"]) / 14)
        words = _fallback_words(ch["text"], dur)
        abs_words = [{"w": w["w"], "start": cursor + w["off"], "end": cursor + w["off"] + w["dur"]} for w in words]
        timed.append({**ch, "audio": audio, "dur": dur, "start": cursor, "end": cursor + dur, "words": abs_words})
        cursor += dur
    return timed


def _moss_timed(chunks: list[dict], voice: str, lang: str, workdir: pathlib.Path,
                speed: float = 1.0) -> list[dict]:
    """MOSS-TTS-Nano в .venv-moss: локальный CPU-TTS + клон голоса из 6-сек сэмпла (ref_audio).
    Контракт идентичен XTTS-воркеру. ref_audio — из env MOSS_REF_AUDIO или из voice (если это путь к wav)."""
    if not core.MOSS_VENV_PY.exists():
        raise RuntimeError(f"Нет MOSS venv ({core.MOSS_VENV_PY}). Установи: bash setup_moss.sh")
    ref_audio = os.environ.get("MOSS_REF_AUDIO", "")
    if not ref_audio and voice and os.path.exists(voice):
        ref_audio = voice
    inp = workdir / "moss_in.json"
    outp = workdir / "moss_out.json"
    inp.write_text(json.dumps({
        "speaker": voice, "ref_audio": ref_audio, "lang": lang, "workdir": str(workdir),
        "chunks": [{"text": c["text"]} for c in chunks],
    }, ensure_ascii=False), encoding="utf-8")
    core.run([str(core.MOSS_VENV_PY), str(core.MOSS_WORKER), str(inp), str(outp)], timeout=3600)
    data = json.loads(outp.read_text(encoding="utf-8"))
    if data.get("error"):
        raise RuntimeError(f"MOSS воркер: {data['error']}")
    res = data["chunks"]
    if len(res) != len(chunks):
        raise RuntimeError(f"MOSS вернул {len(res)} аудио из {len(chunks)} кусков")
    timed: list[dict] = []
    cursor = 0.0
    for i, (ch, pc) in enumerate(zip(chunks, res)):
        audio = pc["audio"]
        if speed and abs(speed - 1.0) > 0.01:
            sped = workdir / f"voice_{i:02d}_spd.wav"
            core.run(["ffmpeg", "-y", "-i", audio, "-filter:a", _atempo_chain(speed), str(sped)])
            audio = str(sped)
        dur = core.media_duration(audio)
        if dur <= 0:
            dur = max(1.0, len(ch["text"]) / 14)
        words = _fallback_words(ch["text"], dur)
        abs_words = [{"w": w["w"], "start": cursor + w["off"], "end": cursor + w["off"] + w["dur"]} for w in words]
        timed.append({**ch, "audio": audio, "dur": dur, "start": cursor, "end": cursor + dur, "words": abs_words})
        cursor += dur
    return timed


# ──────────────────────────── ElevenLabs (каскад ключей) ────────────────────────────

# Имя → voice_id (голоса, доступные free-API). Если передан уже voice_id — используем как есть.
EL_VOICES = {
    "Sarah": "EXAVITQu4vr4xnSDxMaL", "Adam": "pNInz6obpgDQGcFmaJgB",
    "George": "JBFqnCBsd6RMkjVDRZzb", "Charlie": "IKne3meq5aSn9XLyUdCD",
    "Alice": "Xb7hH8MSUJpSbSDYk0k2", "Eric": "cjVigY5qzO86Huf0OWal",
}
EL_GENDER = {"Sarah": "f", "Alice": "f", "Adam": "m", "George": "m", "Charlie": "m", "Eric": "m"}
# Дефолт — Flash v2.5 (выбран по сравнению: отличная скорость голоса, вдвое экономнее).
# Переключается env ELEVENLABS_MODEL (напр. eleven_multilingual_v2 для макс-качества).
# Читается на КАЖДЫЙ вызов (после load_local_secrets), не на импорте — порядок импортов неважен.
def _el_model() -> str:
    return os.environ.get("ELEVENLABS_MODEL", "eleven_flash_v2_5")

EL_SETTINGS = {"stability": 0.45, "similarity_boost": 0.75, "style": 0.4, "use_speaker_boost": True}
# kid (НЕПРОЗРАЧНЫЙ хеш ключа) → unix-время, до которого ключ «остыл». Сырые ключи на диск НЕ пишем.
_EL_COOLDOWN: dict[str, float] = {}
_EL_COOLDOWN_LOADED = False           # одноразовая подгрузка персиста между cron-запусками


def _el_kid(key: str) -> str:
    """Непрозрачный идентификатор ключа для cooldown (НЕ сам секрет): sha1[:10]."""
    return hashlib.sha1(key.encode()).hexdigest()[:10]


def _el_load_cooldown() -> None:
    """Подтянуть персист EL-cooldown один раз за процесс (иначе долбим исчерпанные ключи каждый cron-запуск)."""
    global _EL_COOLDOWN_LOADED
    if _EL_COOLDOWN_LOADED:
        return
    try:
        _EL_COOLDOWN.update(core.load_cooldown("elevenlabs"))
    except Exception as e:  # noqa: BLE001
        core.log_error("el_load_cooldown", e)
    _EL_COOLDOWN_LOADED = True


def _el_set_cooldown(kid: str, until: float) -> None:
    """Выставить cooldown ключу: in-memory + персист на диск (по НЕПРОЗРАЧНОМУ kid)."""
    _EL_COOLDOWN[kid] = until
    try:
        core.save_cooldown("elevenlabs", {kid: until})
    except Exception as e:  # noqa: BLE001
        core.log_error("el_save_cooldown", e)


def _el_keys() -> list[str]:
    raw = os.environ.get("ELEVENLABS_API_KEY", "")
    return [k.strip() for k in raw.split(",") if k.strip()]


def _el_voice_id(voice: str) -> str:
    return EL_VOICES.get(voice, voice)  # имя из таблицы или сырой id


def _el_synth_one(text: str, voice_id: str, out_mp3: pathlib.Path) -> list[dict]:
    """Озвучить кусок через EL с ротацией ключей. Возвращает words[{w,off,dur}] из alignment.
    Бросает RuntimeError, если все ключи исчерпаны/недоступны (→ фолбэк выше)."""
    keys = _el_keys()
    if not keys:
        raise RuntimeError("нет ELEVENLABS_API_KEY")
    _el_load_cooldown()                            # персист cooldown между cron-запусками
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/with-timestamps?output_format=mp3_44100_128"
    body = {"text": text, "model_id": _el_model(), "voice_settings": EL_SETTINGS}
    now = time.time()
    last = "нет доступных ключей"
    for key in keys:
        kid = _el_kid(key)                         # cooldown индексируется НЕПРОЗРАЧНЫМ хешем, не сырым ключом
        if _EL_COOLDOWN.get(kid, 0) > now:
            continue
        try:
            r = requests.post(url, headers={"xi-api-key": key, "Content-Type": "application/json"},
                              json=body, timeout=90)
        except Exception as e:  # noqa: BLE001
            last = f"сеть: {e}"; continue
        if r.status_code == 200:
            data = r.json()
            out_mp3.write_bytes(base64.b64decode(data["audio_base64"]))
            return _el_words_from_alignment(data.get("alignment") or data.get("normalized_alignment"))
        if r.status_code in (401, 402, 403):       # ключ исчерпан/невалиден — надолго в cooldown
            _el_set_cooldown(kid, now + 6 * 3600)
            last = f"HTTP {r.status_code}"
        elif r.status_code == 429:                 # rate limit — короткий cooldown
            _el_set_cooldown(kid, now + 30)
            last = "429 rate limit"
        else:
            last = f"HTTP {r.status_code}: {r.text[:120]}"
    raise RuntimeError(f"ElevenLabs: все ключи недоступны ({last})")


def _el_words_from_alignment(al: dict | None) -> list[dict]:
    """Собрать слова из посимвольного alignment EL: {characters, character_start_times_seconds, ...}."""
    if not al:
        return []
    chars = al.get("characters") or []
    starts = al.get("character_start_times_seconds") or []
    ends = al.get("character_end_times_seconds") or []
    if not (len(chars) == len(starts) == len(ends)) or not chars:
        return []
    words, cur, w_start = [], "", None
    for ch, st, en in zip(chars, starts, ends):
        if ch.isspace():
            if cur:
                words.append({"w": cur, "off": w_start, "dur": max(0.05, prev_end - w_start)})
                cur = ""
            continue
        if not cur:
            w_start = st
        cur += ch
        prev_end = en
    if cur:
        words.append({"w": cur, "off": w_start, "dur": max(0.05, prev_end - w_start)})
    return words


def _elevenlabs_timed(chunks: list[dict], voice: str, workdir: pathlib.Path) -> list[dict]:
    voice_id = _el_voice_id(voice)
    timed: list[dict] = []
    cursor = 0.0
    for i, ch in enumerate(chunks):
        mp3 = workdir / f"voice_{i:02d}.mp3"
        words = _el_synth_one(ch["text"], voice_id, mp3)   # бросит → фолбэк в synthesize
        dur = core.media_duration(mp3)
        if dur <= 0:
            dur = (words[-1]["off"] + words[-1]["dur"]) if words else max(1.0, len(ch["text"]) / 14)
        if not words:
            words = _fallback_words(ch["text"], dur)
        abs_words = [{"w": w["w"], "start": cursor + w["off"], "end": cursor + w["off"] + w["dur"]} for w in words]
        timed.append({**ch, "audio": str(mp3), "dur": dur, "start": cursor, "end": cursor + dur, "words": abs_words})
        cursor += dur
    return timed


# ──────────────────────────── общий код ────────────────────────────

def _concat(timed: list[dict], workdir: pathlib.Path) -> tuple[pathlib.Path, float]:
    # анти-рассинхрон: ДО склейки проверяем КАЖДЫЙ кусок по РЕАЛЬНОМУ файлу (не по timed["dur"]).
    # Битый/нулевой кусок не пропускаем тихо — падаем (рассинхрон субтитров хуже краша: фабрика пересоберёт).
    for i, c in enumerate(timed):
        ap = c.get("audio", "")
        if not ap or not pathlib.Path(ap).exists() or core.media_duration(ap) <= 0:
            raise RuntimeError(f"озвучка: битый кусок #{i}: {ap}")
    list_file = workdir / "audio_list.txt"
    list_file.write_text("".join(f"file '{c['audio']}'\n" for c in timed), encoding="utf-8")
    full = workdir / "narration.m4a"
    # лонгформ до ~12 мин аудио — таймаут склейки с запасом (дефолт 600с впритык на слабом CI)
    core.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
        "-c:a", "aac", "-b:a", "192k", "-ar", "44100", str(full),
    ], timeout=1200)
    total = core.media_duration(full)
    # анти-«немой/битый ролик»: если озвучка пустая/слишком короткая — это сбой движка, не продолжаем
    size = full.stat().st_size if full.exists() else 0
    if not full.exists() or total < 1.0 or size < 5000:
        raise RuntimeError(f"озвучка пустая/битая (dur={total:.2f}s, size={size}б) — сбой TTS")
    return full, total


def _groq_align(audio_path: pathlib.Path, lang: str = "en") -> list[dict]:
    """Точные пословные тайм-коды по РЕАЛЬНОМУ аудио через Groq Whisper (word timestamps).
    Возвращает [{w,start,end}] (абсолютные секунды) или [] при сбое/без ключа.
    Это ground-truth: субтитры лягут идеально в такт даже на фолбэк-движках (edge/XTTS)."""
    key = core.secret("GROQ_API_KEY", required=False)
    if not key or not audio_path.exists():
        return []
    try:
        import requests
        with audio_path.open("rb") as f:
            r = requests.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {key}"},
                files={"file": (audio_path.name, f, "audio/m4a")},
                data={"model": "whisper-large-v3-turbo", "response_format": "verbose_json",
                      "timestamp_granularities[]": "word", "language": ("ru" if lang == "ru" else "en")},
                timeout=300,  # лонгформ: файл ~17МБ и транскрипция 10-12 мин аудио — 90с мало
            )
        if r.status_code != 200:
            core.log(f"groq_align HTTP {r.status_code}", level="warn")
            return []
        words = r.json().get("words") or []
        return [{"w": w.get("word", "").strip(), "start": float(w["start"]), "end": float(w["end"])}
                for w in words if w.get("word", "").strip()]
    except Exception as e:  # noqa: BLE001
        core.log_error("groq_align", e)
        return []


def _apply_alignment(timed: list[dict], gw: list[dict]) -> list[dict]:
    """Переразложить точные groq-слова по кускам. КАЖДОЕ слово — РОВНО в один кусок (по началу слова),
    иначе при перекрывающихся окнах слово на границе дублировалось в субтитрах (баг аудита #4)."""
    if not gw:
        return timed
    buckets: dict[int, list] = {i: [] for i in range(len(timed))}
    for w in gw:
        idx = None
        for i, ch in enumerate(timed):
            if ch["start"] - 0.05 <= w["start"] < ch["end"]:
                idx = i
                break
        if idx is None:   # слово вне всех интервалов → ближайший кусок по центру
            idx = min(range(len(timed)),
                      key=lambda i: abs((timed[i]["start"] + timed[i]["end"]) / 2 - w["start"]))
        buckets[idx].append(w)
    for i, ch in enumerate(timed):
        if buckets[i]:
            ch["words"] = buckets[i]
    return timed


def synthesize(chunks: list[dict], voice: str, rate: str, workdir: pathlib.Path,
               engine: str = "kokoro", lang: str = "en", speed: float = 1.0) -> tuple[list[dict], pathlib.Path, float]:
    """Озвучить куски выбранным движком. Возвращает (timed_chunks, full_audio_path, total_dur).

    Дефолт фабрики — kokoro (локальный, бесплатный). При сбое любого движка НЕ теряем ролик:
    откатываемся на edge-tts (быстрый и надёжный).
    """
    workdir.mkdir(parents=True, exist_ok=True)
    if engine == "kokoro":
        try:
            timed = _kokoro_timed(chunks, voice, workdir, speed=speed)
        except Exception as e:  # noqa: BLE001
            # kokoro не сработал (нет модели/сети, битая закачка) — откат на edge того же пола
            fb = "en-US-AriaNeural" if (voice or "").startswith("af_") else "en-US-AndrewNeural"
            print(f"⚠️  Kokoro не сработал ({str(e)[:160]}); откат на edge {fb}")
            timed = _edge_timed(chunks, fb, rate or "+0%", workdir)
    elif engine == "elevenlabs":
        try:
            timed = _elevenlabs_timed(chunks, voice, workdir)
        except Exception as e:  # noqa: BLE001
            # все EL-ключи исчерпаны/ошибка — не теряем ролик, падаем на edge нужного пола
            gender = EL_GENDER.get(voice, "m")
            if lang == "ru":
                fb = "ru-RU-SvetlanaNeural" if gender == "f" else "ru-RU-DmitryNeural"
            else:
                fb = "en-US-AriaNeural" if gender == "f" else "en-US-AndrewNeural"
            print(f"⚠️  ElevenLabs недоступен ({str(e)[:160]}); откат на edge {fb}")
            timed = _edge_timed(chunks, fb, "+6%", workdir)
    elif engine == "xtts":
        try:
            timed = _xtts_timed(chunks, voice, lang, workdir, speed=speed)
        except Exception as e:  # noqa: BLE001
            print(f"⚠️  XTTS не сработал ({str(e)[:160]}); откат на edge-tts")
            fb = "ru-RU-DmitryNeural" if lang == "ru" else "en-US-AndrewNeural"
            timed = _edge_timed(chunks, fb, "+8%", workdir)
    elif engine == "moss":
        try:
            timed = _moss_timed(chunks, voice, lang, workdir, speed=speed)
        except Exception as e:  # noqa: BLE001
            print(f"⚠️  MOSS не сработал ({str(e)[:160]}); откат на edge-tts")
            fb = "ru-RU-DmitryNeural" if lang == "ru" else "en-US-AndrewNeural"
            timed = _edge_timed(chunks, fb, "+8%", workdir)
    else:
        timed = _edge_timed(chunks, voice, rate, workdir)
    full_audio, total = _concat(timed, workdir)
    # Точное выравнивание субтитров по реальному аудио (Groq Whisper word-timestamps).
    # Можно отключить: CF_GROQ_ALIGN=0. При сбое — тихо остаёмся на таймингах движка.
    if os.environ.get("CF_GROQ_ALIGN", "1") != "0":
        gw = _groq_align(full_audio, lang=lang)
        if gw:
            timed = _apply_alignment(timed, gw)
            core.log(f"субтитры выровнены по аудио (Groq): {len(gw)} слов", level="info")
    return timed, full_audio, total


if __name__ == "__main__":
    core.load_local_secrets()
    demo = [
        {"text": "In 1347, a single merchant ship changed the course of European history.", "broll_query": "medieval ship", "role": "hook"},
        {"text": "Within five years, nearly half the continent's population was gone.", "broll_query": "medieval city", "role": "body"},
        {"text": "This is the story of the Black Death, and how it reshaped the world.", "broll_query": "old map", "role": "body"},
    ]
    eng = sys.argv[1] if len(sys.argv) > 1 else "kokoro"
    _dflt = {"kokoro": KOKORO_DEFAULT_VOICE, "xtts": "Luis Moray", "elevenlabs": "Adam"}
    vc = sys.argv[2] if len(sys.argv) > 2 else _dflt.get(eng, "en-US-AndrewNeural")
    wd = core.OUTPUT_DIR / "_voice_test"
    timed, audio, total = synthesize(demo, vc, "+0%", wd, engine=eng, lang="en")
    print(f"[{eng}] {audio} · {total:.2f}s")
    for t in timed:
        print(f"  [{t['start']:.2f}-{t['end']:.2f}] {len(t['words'])} слов · {t['text'][:48]}")
