"""Генерация фоновых КАРТИНОК под фразы сценария (b-roll вместо стока).

Каскад: Gemini 2.5/3.x Flash Image («nano-banana») → Pollinations FLUX (бесплатно, без ключа) → None.
Картинку потом оживляет Ken Burns в assemble.
⚠️ ПРОВЕРЕНО 2026-06-16: бесплатная Gemini-генерация картинок нам НЕДОСТУПНА — даже одиночный
запрос после паузы отдаёт 429 (free_tier_requests / free_tier_input_token_count = исчерпано/0;
совпадает с тем, что Google убрал free tier для image-моделей). Реальный рабочий путь = Pollinations
(FLUX, без ключа, проверено). Gemini-слот оставлен: включится сам, если на ключ подключат биллинг.
"""
import os
import re
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
DAILY_LIMITS = {"nvidia": 40, "cloudflare": 250, "pollinations": 120, "gemini": 0}
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
    fresh = {"date": core.today_str(), "nvidia": 0, "cloudflare": 0, "pollinations": 0, "gemini": 0}
    try:
        if _quota_path.exists():
            d = json.loads(_quota_path.read_text(encoding="utf-8"))
            if isinstance(d, dict) and d.get("date") == core.today_str():
                for k in ("nvidia", "cloudflare", "pollinations", "gemini"):
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


_HUMAN_WORDS = (r"\b(crowds?|people|persons?|man|men|woman|women|kids?|children|boys?|girls?|astronauts?|"
                r"astronomers?|scientists?|engineers?|workers?|drivers?|pilots?|tourists?|spectators?|"
                r"residents?|locals|villagers|families|hands?|faces?|onlookers?|watching|pointing|holding|looking at|"
                r"standing|walking)\b")


def prompt_for(query: str, niche: dict, character: str = "") -> str:
    """EN-запрос сегмента → полноценный промпт фотореалистичной сцены под АКТИВНЫЙ формат
    (core.ACTIVE_FORMAT в момент вызова: long → 16:9 widescreen, short → 9:16 vertical).
    Если задан character (story-ниши) — он идёт ПЕРВЫМ и повторяется в каждом кадре,
    чтобы персонаж/предмет был один и тот же во всём ролике (визуальная связность)."""
    q = (query or niche.get("broll_hint", "person")).strip()
    style = niche.get("image_style")
    if style:   # ниши сети: у каждого канала свой узнаваемый стиль (анти-«однообразный контент»)
        orient = ("16:9 widescreen frame, wide composition" if core.ACTIVE_FORMAT == "long" else
                  "9:16 vertical frame, tall vertical composition, subject centered and fully inside the frame")
        # Каналы сети людей не рисуют, а сценарий всё равно просит «crowds watching…» — FLUX тогда
        # рисует толпу и кадр уходит в брак. Убираем людей из сцены, остаётся обстановка.
        q = re.sub(_HUMAN_WORDS, "", q, flags=re.I)
        q = re.sub(r"\s{2,}", " ", q).strip(" ,") or niche.get("broll_hint", "landscape")
        subj = f"{character}. Scene: {q}" if character else q
        return (f"{style['prefix']}, {subj}, {style.get('suffix', '')}, "
                "no text, no letters, no captions, no signage, no logos, no watermark, "
                f"anatomically correct, {orient}")
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


def _strip_watermark(out: pathlib.Path) -> None:
    """Pollinations ставит «pollinations.ai» в правый нижний угол (и анонимно, и с ключом, проверено
    2026-11-12). Срезаем нижние 7% с сохранением пропорций и возвращаем размер активного формата."""
    try:
        from PIL import Image
        im = Image.open(out).convert("RGB")
        w, h = im.size
        nh = int(h * 0.93)
        nw = int(w * 0.93)
        x0 = (w - nw) // 2
        im.crop((x0, 0, x0 + nw, nh)).resize((core.W, core.H), Image.LANCZOS).save(out, format="PNG")
    except Exception as e:  # noqa: BLE001
        core.log_error("imagegen.strip_watermark", e)


def _polli_anon_get(url: str, headers: dict, waits: int = 3) -> bytes:
    """Анонимный Pollinations: лимит «1 картинка в 45 с на IP» (402 FREE_LIMIT, 2026-11-12).
    Ждём указанный Retry after и повторяем, а не бросаем слот в сток."""
    import re as _re
    for _ in range(waits + 1):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=120) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")
            if e.code != 402 or "FREE_LIMIT" not in body:
                raise
            m = _re.search(r"Retry after (\d+)s", body)
            time.sleep(min(60, int(m.group(1)) + 2 if m else 47))
    raise RuntimeError("Pollinations FREE_LIMIT не отпустил")


def _pollinations_image(prompt: str, out: pathlib.Path, seed: int = 0) -> bool:
    """Бесплатно без ключа (fair use). Размер = core.W x core.H активного формата (динамически,
    в момент вызова). Пробует модели с лучшей анатомией (nanobanana) → базовую (flux).
    _too_dark/битый ответ одной модели → следующая."""
    q = urllib.parse.quote(prompt)
    # Несколько ключей через запятую: у каждого свой бесплатный tier-баланс (~0.25 pollen/сутки)
    keys = [k.strip() for k in os.environ.get("POLLINATIONS_API_KEY", "").split(",") if k.strip()]
    sk = keys[0] if keys else ""
    headers = {"User-Agent": "content-factory"}
    if sk:
        headers["Authorization"] = f"Bearer {sk}"
    # СНАЧАЛА авторизованный gen.pollinations.ai: image.pollinations.ai ставит водяной знак
    # «pollinations.ai» в угол даже с nologo (проверено 2026-11-12). Списывает
    # бесплатный tier-баланс аккаунта, ~0.002 pollen за flux-кадр; проверено 2026-10-05).
    # Баланс ключа кончился (402) или сбой → следующий ключ.
    params = urllib.parse.urlencode({"width": core.W, "height": core.H, "model": "flux",
                                     "seed": seed, "nologo": "true", "private": "true"})
    for key in keys:
        try:
            req = urllib.request.Request(f"https://gen.pollinations.ai/image/{q}?{params}",
                                         headers={**headers, "Authorization": f"Bearer {key}"})
            with urllib.request.urlopen(req, timeout=120) as r:
                data = r.read()
            if len(data) > 5000:
                out.write_bytes(data)
                if not _too_dark(out):
                    _strip_watermark(out)
                    return True
        except Exception:  # noqa: BLE001
            continue
    # nanobanana (лучшие руки) анонимно 500-ит → пробуем его только при наличии ключа; иначе сразу flux
    models = ("flux",)   # анонимно nanobanana 500-ит; платные модели идут через gen.pollinations выше
    for model in models:
        params = urllib.parse.urlencode({"width": core.W, "height": core.H, "model": model,
                                         "seed": seed, "nologo": "true", "private": "true"})
        url = f"https://image.pollinations.ai/prompt/{q}?{params}"
        try:
            data = _polli_anon_get(url, headers)
            if len(data) <= 5000:
                continue
            out.write_bytes(data)
            if _too_dark(out):        # чёрный/битый кадр → пробуем следующую модель
                continue
            _strip_watermark(out)
            return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _cloudflare_accounts() -> list[tuple[str, str]]:
    """(account_id, token) из llm_providers.json (cloudflare-N) + env CLOUDFLARE_AI_TOKEN_N."""
    try:
        provs = json.loads((pathlib.Path(__file__).resolve().parent.parent / "llm_providers.json")
                           .read_text())["providers"]
    except Exception:  # noqa: BLE001
        return []
    out = []
    for pr in provs:
        url, env = pr.get("base_url", ""), pr.get("key_env", "")
        if "api.cloudflare.com/client/v4/accounts/" in url and os.environ.get(env, "").strip():
            out.append((url.split("/accounts/")[1].split("/")[0], os.environ[env].strip()))
    return out


def _cloudflare_flux_image(prompt: str, out: pathlib.Path, seed: int = 0) -> bool:
    """FLUX.1-schnell на Cloudflare Workers AI (бесплатные нейроны, 5 аккаунтов). Отдаёт только
    1024×1024 → центр-кроп под активный формат (9:16 / 16:9) и масштаб до core.W×core.H.
    seed API не принимает — разнообразие даёт сам сэмплер."""
    from PIL import Image
    import io
    accs = _cloudflare_accounts()
    if not accs:
        return False
    start = seed % len(accs)
    for acc, tok in accs[start:] + accs[:start]:
        cid = "cf:" + _kh(tok)
        if time.time() < _COOLDOWN.get(cid, 0):
            continue
        try:
            req = urllib.request.Request(
                f"https://api.cloudflare.com/client/v4/accounts/{acc}/ai/run/@cf/black-forest-labs/flux-1-schnell",
                data=json.dumps({"prompt": prompt[:2000], "steps": 8}).encode(),
                headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
            d = json.loads(urllib.request.urlopen(req, timeout=90).read())
            img = (d.get("result") or {}).get("image")
            if not img:
                continue
            im = Image.open(io.BytesIO(base64.b64decode(img))).convert("RGB")
            w, h = im.size
            ratio = core.W / core.H
            if ratio < w / h:
                cw, ch = int(h * ratio), h
            else:
                cw, ch = w, int(w / ratio)
            x0, y0 = (w - cw) // 2, (h - ch) // 2
            im.crop((x0, y0, x0 + cw, y0 + ch)).resize((core.W, core.H), Image.LANCZOS).save(out, format="PNG")
            if _too_dark(out):
                continue
            return True
        except urllib.error.HTTPError as e:
            if e.code in (429, 402, 403):
                _COOLDOWN[cid] = time.time() + 3600   # нейроны аккаунта кончились → следующий
            continue
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
    if _quota_load().get("cloudflare", 0) < DAILY_LIMITS.get("cloudflare", 0):
        if _cloudflare_flux_image(prompt, out, seed=seed):
            _quota_bump("cloudflare")
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


def frame_check(path: str, niche: dict, query: str = "") -> dict:
    """Проверка ОДНОГО AI-кадра до монтажа (Gemini vision, ключи по кругу).
    {ok, issues, checked}. Нет ключей / все ключи упали → ok=True, checked=False (не роняем ран)."""
    keys = [k.strip() for k in os.environ.get("GEMINI_API_KEY", "").split(",") if k.strip()]
    if not keys:
        return {"ok": True, "issues": [], "checked": False}
    style = (niche.get("image_style") or {}).get("prefix", "")
    rules = "; ".join(niche.get("frame_rules") or [])
    if style:   # судим по той же сцене, что ушла во FLUX (люди вырезаны в prompt_for)
        query = re.sub(r"\s{2,}", " ", re.sub(_HUMAN_WORDS, "", query or "", flags=re.I)).strip(" ,")
    # Сцена — мягкий критерий: FLUX-schnell в стилизации не рисует точную сцену/вид, а lite-модель
    # при общем вердикте «ok» браковала почти всё за неточный вид. Поэтому модель ставит отдельные
    # флаги и оценку сюжета 0-10, а решение принимает код: брак = любой жёсткий дефект или сюжет < 4.
    prompt = ("You inspect one AI-generated b-roll frame for a faceless YouTube channel. Answer each flag "
              "honestly with true/false. "
              "text: letters, words, numbers, captions, signatures, logos or watermarks a viewer would notice "
              "(tiny unreadable markings, buttons or tick marks on a device are NOT text). "
              "deformed: melted or nonsensical blobs, wrong limbs, broken faces or bodies (simplified, stylized, "
              "toy-like or exploded-view parts are NOT deformed). "
              "people: any human or human body part. "
              "blank: mostly blank, black or blurred frame. "
              "wrong_style: clearly a different art style than expected (minor variations are fine). "
              "rule_violation: something VISIBLE in the frame breaks one of the extra channel rules below "
              "(a missing scene element is never a rule violation). "
              "relevance: 0-10, how well the frame fits the TOPIC of the requested scene as b-roll; extra "
              "objects, missing details, a different species/model or a loose interpretation still score 5+, "
              "only a frame about a different subject altogether scores below 4. People, crowds, hands, text, "
              "labels and maps with names are deliberately never drawn: if the scene asks for them, judge only "
              "the remaining setting and do not lower relevance for their absence. "
              f"Requested scene: {query}. Expected style: {style}. Extra channel rules: {rules or 'none'}. "
              'Return STRICTLY JSON {"text": bool, "deformed": bool, "people": bool, "blank": bool, '
              '"wrong_style": bool, "rule_violation": bool, "relevance": int, "issues": ["short notes"]}.')
    try:
        data = base64.b64encode(pathlib.Path(path).read_bytes()).decode()
    except OSError:
        return {"ok": False, "issues": ["файл кадра не читается"], "checked": True}
    mime = "image/png" if str(path).endswith(".png") else "image/jpeg"
    body = json.dumps({"contents": [{"parts": [{"text": prompt},
                                               {"inline_data": {"mime_type": mime, "data": data}}]}],
                       "generationConfig": {"responseMimeType": "application/json"}}).encode()
    import random
    random.shuffle(keys)
    # 2.5-flash: бесплатный дневной лимит кончается за пару тестовых ранов (429 на всех ключах),
    # lite-модели — отдельная квота и 3-6 с на кадр. 2.0-flash / 2.5-flash-lite → 404.
    for model in ("gemini-3.1-flash-lite", "gemini-flash-lite-latest", "gemini-2.5-flash"):
        for key in keys:
            try:
                req = urllib.request.Request(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                    data=body, headers={"Content-Type": "application/json", "x-goog-api-key": key})
                r = json.loads(urllib.request.urlopen(req, timeout=40).read())
                txt = r["candidates"][0]["content"]["parts"][0]["text"]
                d = json.loads(txt)
                if isinstance(d, list) and d and isinstance(d[0], dict):
                    d = d[0]
                if not isinstance(d, dict) or "relevance" not in d:
                    continue                      # не та схема — это не вердикт, спрашиваем дальше
                flags = [f for f in ("text", "deformed", "people", "blank", "wrong_style", "rule_violation")
                         if str(d.get(f)).strip().lower() in ("true", "1", "yes")]
                try:
                    rel = float(d["relevance"])
                    if rel > 10:                  # модель ответила по шкале 0-100
                        rel /= 10
                except (TypeError, ValueError):
                    rel = 10.0
                if rel < 4:
                    flags.append(f"unrelated:{d['relevance']}")
                notes = d.get("issues") or []
                notes = [notes] if isinstance(notes, str) else [str(x) for x in notes]
                return {"ok": not flags, "issues": flags + notes[:3], "checked": True, "model": model}
            except Exception:  # noqa: BLE001 — 429/401/сеть/непарсибельно → следующий ключ/модель
                continue
    return {"ok": True, "issues": [], "checked": False}


def generate(query: str, niche: dict, out: pathlib.Path, seed: int = 0, character: str = "") -> str | None:
    """Сгенерировать b-roll картинку под фразу: prompt_for(query) → каскад generate_raw."""
    return generate_raw(prompt_for(query, niche, character=character), out, seed=seed)


if __name__ == "__main__":
    core.load_local_secrets()
    p = generate("tired young man procrastinating on couch with phone",
                 core.get_niche("mind_facts"), core.OUTPUT_DIR / "_imgtest.png", seed=7)
    print("результат:", p)
