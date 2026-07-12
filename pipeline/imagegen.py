"""Генерация фоновых КАРТИНОК под фразы сценария (b-roll вместо стока).

Каскад: Gemini 2.5/3.x Flash Image («nano-banana») → Pollinations FLUX (бесплатно, без ключа) → None.
Картинку потом оживляет Ken Burns в assemble.
⚠️ ПРОВЕРЕНО 2026-06-16: бесплатная Gemini-генерация картинок нам НЕДОСТУПНА — даже одиночный
запрос после паузы отдаёт 429 (free_tier_requests / free_tier_input_token_count = исчерпано/0;
совпадает с тем, что Google убрал free tier для image-моделей). Реальный рабочий путь = Pollinations
(FLUX, без ключа, проверено). Gemini-слот оставлен: включится сам, если на ключ подключат биллинг.
"""
import os
import time
import json
import base64
import hashlib
import pathlib
import urllib.parse
import urllib.request
import urllib.error

import sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import core  # noqa: E402

GEMINI_IMG_MODEL = "gemini-2.5-flash-image"

# Дневные лимиты картинок по провайдеру (gemini мёртв на free-tier → 0 = всегда пропускать).
DAILY_LIMITS = {"nvidia": 40, "pollinations": 120, "gemini": 0}
_quota_path = core.CACHE_DIR / "imagegen_quota.json"

# In-memory cooldown по непрозрачным ключам (sha1[:12]) — на диск сырые секреты НЕ пишем.
_COOLDOWN: dict[str, float] = {}
try:
    _COOLDOWN.update(core.load_cooldown("imagegen"))
except Exception:  # noqa: BLE001
    pass


def _kh(key: str) -> str:
    """Непрозрачный хеш ключа для cooldown-id."""
    return hashlib.sha1(key.encode()).hexdigest()[:12]


def _cooldown_set(cid: str, until: float) -> None:
    """Выставить cooldown по непрозрачному id (cid вида 'g:<sha1>' / 'nv:<sha1>') + персист на диск."""
    _COOLDOWN[cid] = until
    try:
        core.save_cooldown("imagegen", {cid: until})
    except Exception as e:  # noqa: BLE001
        core.log_error("imagegen._cooldown_set", e)


def _quota_load() -> dict:
    """Дневной счётчик картинок. При отсутствии/повреждении/смене даты → свежий dict с нулями."""
    fresh = {"date": core.today_str(), "nvidia": 0, "pollinations": 0, "gemini": 0}
    try:
        if _quota_path.exists():
            d = json.loads(_quota_path.read_text(encoding="utf-8"))
            if isinstance(d, dict) and d.get("date") == core.today_str():
                for k in ("nvidia", "pollinations", "gemini"):
                    fresh[k] = int(d.get(k, 0) or 0)
                return fresh
    except Exception as e:  # noqa: BLE001
        core.log_error("imagegen._quota_load", e)
    return fresh


def _quota_bump(provider: str) -> None:
    """+1 к дневному счётчику провайдера (вызывать ТОЛЬКО при успешной генерации)."""
    try:
        d = _quota_load()
        d[provider] = int(d.get(provider, 0) or 0) + 1
        _quota_path.parent.mkdir(parents=True, exist_ok=True)
        _quota_path.write_text(json.dumps(d), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        core.log_error("imagegen._quota_bump", e)


def _gemini_keys() -> list[str]:
    return [k.strip() for k in os.environ.get("GEMINI_API_KEY", "").split(",") if k.strip()]


def prompt_for(query: str, niche: dict, character: str = "") -> str:
    """EN-запрос сегмента → полноценный промпт фотореалистичной сцены под АКТИВНЫЙ формат
    (core.ACTIVE_FORMAT в момент вызова: long → 16:9 widescreen, short → 9:16 vertical).
    Если задан character (story-ниши) — он идёт ПЕРВЫМ и повторяется в каждом кадре,
    чтобы персонаж/предмет был один и тот же во всём ролике (визуальная связность)."""
    q = (query or niche.get("broll_hint", "person")).strip()
    if core.ACTIVE_FORMAT == "long":
        # long-form (историческая реконструкция): широкие сцены-среды, БЕЗ крупных лиц и
        # читаемого текста — урок донора: FLUX ломает лица и надписи/бренды. Стиль темы
        # приходит из broll_hint через query (сцена сегмента) и fallback выше.
        return (f"photorealistic cinematic wide establishing shot, {q}, "
                f"environments and objects in focus, human figures only distant or seen from behind, "
                f"no close-up faces, epic painterly composition, rich colors, cinematic color grading, "
                f"natural dramatic lighting, shot on film, 35mm, highly detailed, "
                f"no text, no letters, no signage, no brand logos, no watermark, "
                f"16:9 widescreen cinematic frame")
    if character:
        return (f"photorealistic cinematic vertical photograph. {character}. Scene: {q}. "
                f"consistent character, expressive emotion, dramatic cinematic lighting, rich color grading, "
                f"shot on film, 35mm, shallow depth of field, highly detailed, "
                f"no text, no watermark, 9:16 vertical")
    return (f"photorealistic cinematic vertical photograph, {q}, real people, candid, "
            f"striking composition, rich colors, cinematic color grading, sharp in-focus subject, "
            f"natural dramatic lighting, shot on film, 35mm, shallow depth of field, highly detailed, "
            f"no text, no watermark, 9:16 vertical")


def _gemini_image(prompt: str, out: pathlib.Path) -> bool:
    keys = _gemini_keys()
    now = time.time()
    ar = "16:9" if core.ACTIVE_FORMAT == "long" else "9:16"   # аспект по активному формату
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseModalities": ["IMAGE"], "imageConfig": {"aspectRatio": ar}},
    }).encode()
    for key in keys:
        cid = "g:" + _kh(key)
        if _COOLDOWN.get(cid, 0) > now:
            continue
        # ключи формата `AQ.…` требуют заголовок x-goog-api-key (старый ?key= → 401).
        # ПРИМ.: генерация картинок у Gemini платная (free-tier limit:0) — этот путь обычно
        # отваливается по квоте 429 и уходит в фолбэк NVIDIA/Pollinations. Auth чиним для честной ошибки.
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
               f"{GEMINI_IMG_MODEL}:generateContent")
        req = urllib.request.Request(url, data=body,
                                     headers={"Content-Type": "application/json", "x-goog-api-key": key})
        try:
            r = json.loads(urllib.request.urlopen(req, timeout=90).read())
            parts = r.get("candidates", [{}])[0].get("content", {}).get("parts", [])
            data = next((p["inlineData"]["data"] for p in parts if "inlineData" in p), None)
            if data:
                out.write_bytes(base64.b64decode(data))
                return out.stat().st_size > 5000
        except urllib.error.HTTPError as e:  # noqa
            _cooldown_set(cid, now + (3600 if e.code == 429 else 600))
        except Exception:  # noqa: BLE001
            _cooldown_set(cid, now + 120)
    return False


def _too_dark(out: pathlib.Path) -> bool:
    """True если картинка почти чёрная (NVIDIA иногда отдаёт пустой кадр >5КБ, но mean≈0).
    Без PIL — не блокируем (вернём False)."""
    try:
        from PIL import Image, ImageStat
        return ImageStat.Stat(Image.open(out).convert("L")).mean[0] < 8.0
    except Exception:  # noqa: BLE001
        return False


def _nvidia_flux_image(prompt: str, out: pathlib.Path, seed: int = 0) -> bool:
    """FLUX.1-dev через NVIDIA NIM (ключ NVIDIA_API_KEY, без карты). Размер по активному формату:
    long → 1344x768 (16:9), short → нативная вертикаль 768x1344 (9:16). Проверено 2026-06-16:
    работает, фотореализм выше Pollinations. Мульти-ключ + cooldown."""
    keys = [k.strip() for k in os.environ.get("NVIDIA_API_KEY", "").split(",") if k.strip()]
    if not keys:
        return False
    now = time.time()
    w, h = (1344, 768) if core.ACTIVE_FORMAT == "long" else (768, 1344)
    body = json.dumps({"prompt": prompt, "width": w, "height": h, "steps": 30,
                       "cfg_scale": 3.5, "seed": seed, "mode": "base"}).encode()
    for key in keys:
        cid = "nv:" + _kh(key)
        if _COOLDOWN.get(cid, 0) > now:
            continue
        req = urllib.request.Request("https://ai.api.nvidia.com/v1/genai/black-forest-labs/flux.1-dev",
                                     data=body, headers={"Authorization": f"Bearer {key}",
                                                         "Accept": "application/json", "Content-Type": "application/json"})
        try:
            r = json.loads(urllib.request.urlopen(req, timeout=120).read())
            arts = r.get("artifacts") or []
            b64 = arts[0].get("base64") if arts else r.get("image")
            if b64:
                out.write_bytes(base64.b64decode(b64))
                if out.stat().st_size > 5000 and not _too_dark(out):
                    return True
                # пустой/чёрный кадр (бывает у NVIDIA при перегрузе) → не успех, идём к Pollinations
        except urllib.error.HTTPError as e:  # noqa
            _cooldown_set(cid, now + (3600 if e.code in (429, 402) else 600))
        except Exception:  # noqa: BLE001
            _cooldown_set(cid, now + 120)
    return False


# Модели Pollinations в порядке предпочтения по АНАТОМИИ (ресёрч 2026-07: nanobanana=Gemini
# Nano Banana даёт лучшие руки/лица; flux — базовый фолбэк). Пробуем по очереди до первого успеха.
_POLLI_MODELS = ("nanobanana", "flux")


def _pollinations_image(prompt: str, out: pathlib.Path, seed: int = 0) -> bool:
    """Бесплатно без ключа (fair use). Размер = core.W x core.H активного формата (динамически,
    в момент вызова). Пробует модели с лучшей анатомией (nanobanana) → базовую (flux).
    _too_dark/битый ответ одной модели → следующая."""
    q = urllib.parse.quote(prompt)
    sk = os.environ.get("POLLINATIONS_API_KEY", "").strip()
    headers = {"User-Agent": "content-factory"}
    if sk:
        headers["Authorization"] = f"Bearer {sk}"
    # nanobanana (лучшие руки) анонимно 500-ит → пробуем его только при наличии ключа; иначе сразу flux
    models = _POLLI_MODELS if sk else ("flux",)
    for model in models:
        params = urllib.parse.urlencode({"width": core.W, "height": core.H, "model": model,
                                         "seed": seed, "nologo": "true", "private": "true"})
        url = f"https://image.pollinations.ai/prompt/{q}?{params}"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=120) as r:
                data = r.read()
            if len(data) <= 5000:
                continue
            out.write_bytes(data)
            if _too_dark(out):        # чёрный/битый кадр → пробуем следующую модель
                continue
            return True
        except Exception:  # noqa: BLE001
            continue
    return False


def generate_raw(prompt: str, out: pathlib.Path, seed: int = 0) -> str | None:
    """Каскад по ГОТОВОМУ промпту (минуя prompt_for): NVIDIA FLUX (ключ, топ-качество) →
    Pollinations FLUX (без ключа) → Gemini (мёртв на free, на случай биллинга).
    Для обложек/кастомных сцен. Возвращает путь или None."""
    if _quota_load().get("nvidia", 0) < DAILY_LIMITS.get("nvidia", 0):
        if _nvidia_flux_image(prompt, out, seed=seed):
            _quota_bump("nvidia")
            return str(out)
    if _quota_load().get("pollinations", 0) < DAILY_LIMITS.get("pollinations", 0):
        if _pollinations_image(prompt, out, seed=seed):
            _quota_bump("pollinations")
            return str(out)
    if _quota_load().get("gemini", 0) < DAILY_LIMITS.get("gemini", 0):
        if _gemini_image(prompt, out):
            _quota_bump("gemini")
            return str(out)
    return None


def generate(query: str, niche: dict, out: pathlib.Path, seed: int = 0, character: str = "") -> str | None:
    """Сгенерировать b-roll картинку под фразу: prompt_for(query) → каскад generate_raw."""
    return generate_raw(prompt_for(query, niche, character=character), out, seed=seed)


if __name__ == "__main__":
    core.load_local_secrets()
    p = generate("tired young man procrastinating on couch with phone",
                 core.get_niche("mind_facts"), core.OUTPUT_DIR / "_imgtest.png", seed=7)
    print("результат:", p)
