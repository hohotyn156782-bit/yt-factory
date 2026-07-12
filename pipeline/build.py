"""Оркестратор: один вызов = один готовый ролик с метаданными под площадки.

build_video(niche_id, topic) → папка output/<стамп-ниша-слаг>/ с:
  video.mp4         — готовый вертикальный ролик 1080x1920
  subs.ass          — субтитры (вожжены в видео, файл для правок)
  meta.json         — метаданные (title/description/caption/hashtags/platforms/duration)
  POST.txt          — человекочитаемый набор подписей под каждую площадку
  script.json       — исходный сценарий
"""
import os
import json
import pathlib

import sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import core  # noqa: E402
from pipeline import script as scriptmod, voice, broll, subtitles, assemble  # noqa: E402


def _ensure_disk() -> None:
    """Самовосстановление диска перед тяжёлыми шагами (скачивание сток-клипов / рендер ffmpeg).
    Если места мало — агрессивно чистим кэш/output/media и проверяем снова; если и после этого
    мало — пробрасываем понятную ошибку (молчаливый ffmpeg-крах при полном диске хуже)."""
    try:
        core.check_disk()
    except RuntimeError:
        core.cleanup_cache(2)
        core.cleanup_outputs(7)
        core.cleanup_media(21)
        core.check_disk()        # всё ещё мало → пробрасываем


def _pick_music() -> str | None:
    """Трек фоновой музыки из банка (если наполнен; по умолчанию папка пуста — музыка выкл,
    см. pipeline/music.py про Content ID). РОТАЦИЯ: не один и тот же трек на каждом ролике —
    счётчик в state-файле банка перебирает треки по кругу (однообразный фон снижает удержание)."""
    if not core.MUSIC_DIR.exists():
        return None
    tracks = sorted([p for p in core.MUSIC_DIR.iterdir()
                     if p.suffix.lower() in (".mp3", ".m4a", ".wav", ".aac", ".ogg")])
    if not tracks:
        return None
    ctr = core.MUSIC_DIR / ".rotation"          # без аудио-суффикса → не попадёт в список треков
    try:
        idx = int(ctr.read_text().strip()) if ctr.exists() else 0
    except Exception:  # noqa: BLE001
        idx = 0
    pick = tracks[idx % len(tracks)]
    try:
        ctr.write_text(str((idx + 1) % 100000))
    except Exception:  # noqa: BLE001
        pass
    return str(pick)


# Спеки формата под площадку (v2: раздельные ролики). target ≈ длина озвучки (~2.3 слова/сек).
# target держим В ПРЕДЕЛАХ окна validate() (62-92 слов) — иначе сценарий отклоняется и жжёт попытки:
# YouTube длиннее (информативнее ~38с), ig_vk средне (~34с), TikTok короче/динамичнее (~28-30с).
PLATFORM_SPECS = {
    "youtube": {"target": 88, "hint": "Формат YouTube Shorts: чуть длиннее и информативнее, "
                "ценность + интрига, сильный образовательный угол, держи до конца."},
    "tiktok":  {"target": 66, "hint": "Формат TikTok: коротко и динамично, мощный хук в первую "
                "секунду, трендовая разговорная подача, без лишних слов."},
    "ig_vk":   {"target": 80, "hint": "Формат Reels/VK Клипы: эстетично и эмоционально, под "
                "русскую аудиторию, плавный ритм, цепляющая концовка."},
}


def _story_caption(sc: dict, niche: dict | None = None, serial_part: int | None = None) -> str:
    """LLM-подпись для соцсетей (VK/IG/Threads): цепляющий крючок + интрига + вовлечение, по нише,
    без AI-воды. serial_part=1 → концовка «продолжение завтра»; =2 → «это 2-я часть».
    Гейт CF_STORY_CAPTION (по умолч. вкл). Fallback — исходный sc['caption']."""
    if os.environ.get("CF_STORY_CAPTION", "1") == "0":
        return sc.get("caption", "")
    nd = niche or {}
    ctx = json.dumps({"topic": sc.get("topic", ""), "hook": sc.get("hook", ""),
                      "outro": sc.get("outro", ""),
                      "first": (sc.get("segments") or [{}])[0].get("text", "")}, ensure_ascii=False)
    tail = ""
    if serial_part == 1:
        tail = (" Это ПЕРВАЯ часть истории: в самом конце добавь интригу-обещание продолжения "
                "(напр. «Продолжение завтра — не пропусти» / «Часть 2 уже завтра»).")
    elif serial_part == 2:
        tail = " Это ВТОРАЯ часть: в начале коротко напомни, что это продолжение вчерашней истории."
    system = (
        f"Ты пишешь цепляющую подпись под вертикальное видео в нише «{nd.get('title', '')}». "
        "По теме/хуку/сути напиши подпись на ЧИСТОМ русском: 2-4 коротких строки — интригующий крючок "
        "(НЕ спойлерь развязку), одна фраза сути, в конце вовлекающий вопрос или мягкий призыв "
        "(коммент/подписка). Живой тон, на «ты». ЗАПРЕЩЕНО: «создано с помощью ИИ», канцелярит, вода, "
        "«кто бы решился проверить», спам-эмодзи (макс 1-2). Верни ТОЛЬКО текст подписи, без кавычек." + tail)
    try:
        txt = scriptmod._groq(system, "Контекст:\n" + ctx, temp=0.8, max_tokens=300, json_mode=False)
        txt = (txt or "").strip()
        # подстраховка: если провайдер всё же вернул JSON-обёртку — достаём текст
        if txt.startswith("{"):
            try:
                d = json.loads(txt); cap = d.get("caption", d) if isinstance(d, dict) else txt
                if isinstance(cap, dict):
                    cap = "\n".join(cap.get("lines", [])) or ""
                txt = cap if isinstance(cap, str) and cap else txt
            except Exception:  # noqa: BLE001
                pass
        import re as _re
        txt = _re.sub(r"\s*#\S+", "", txt.strip().strip('"')).strip()   # хэштеги добавим отдельно
        return txt or sc.get("caption", "")
    except Exception as e:  # noqa: BLE001
        core.log_error("build._story_caption", e)
        return sc.get("caption", "")


def _build_captions(sc: dict, disclaimer: str = "", ai_used: bool = False,
                    niche: dict | None = None) -> dict:
    """Подписи под площадки по ресёрчу алгоритмов 2026:
    • YouTube: ЧИСТЫЙ заголовок (ключевики важнее); хэштеги — в ОПИСАНИИ, #Shorts ПЕРВЫМ, 3-5 шт
      (>15 → YouTube игнорит ВСЕ). TikTok/IG — шире (до 8). AI-лейбл при AI-визуале (TikTok-комплаенс).
    • YouTube-описание front-load'ит ключевую фразу ПЕРВОЙ строкой (B15) — питает search/suggested
      как второй канал дискавери помимо ленты. Чисто строковая сборка, без LLM.
    """
    raw = [t for t in sc.get("hashtags", []) if t]
    # #Shorts всегда первым, без дублей
    def _ordered(first: str, tags: list[str], n: int) -> str:
        seen, out = set(), [first]
        seen.add(first.lower())
        for t in tags:
            tl = t.lower()
            if tl not in seen:
                seen.add(tl); out.append(t)
            if len(out) >= n:
                break
        return " ".join(out)

    topic = (sc.get("topic", "") or "").strip()
    # #14: тема-специфичный тег #КамелКейс из topic/первого ключевика (1-2 значимых слова) —
    #      вставляем СРАЗУ после #Shorts, перед нишевыми. Дедуп делает _ordered.
    def _topic_tag(text: str) -> str | None:
        import re as _re
        # КИРИЛЛИЦУ сохраняем — RU-аудитория ищет по кириллическим тегам, не транслиту
        _stop = {"как", "что", "это", "для", "или", "почему", "the", "and", "you", "how", "why"}
        words = [w for w in _re.findall(r"\w+", (text or "").lower(), _re.UNICODE)
                 if len(w) > 2 and w not in _stop and not w.isdigit()][:2]
        if not words:
            return None
        return "#" + "".join(w.capitalize() for w in words)      # #КамелКейс (кириллица/латиница)
    topic_tags = [t for t in (_topic_tag(topic),) if t]

    yt_tags = _ordered("#Shorts", topic_tags + raw, 5)
    social_tags = _ordered("#shorts", topic_tags + raw, 8)   # TikTok/IG переваривают больше
    # заголовок — чистый, без хвостовых #shorts/#short (ресёрч: ключевики на первом плане)
    import re as _re
    yt_title = _re.sub(r"\s*#\w+\s*$", "", sc.get("title", "").strip()).strip()[:60]

    ai_note = ("Создано с помощью ИИ. " if ai_used else "")
    pre = (ai_note + disclaimer + "\n\n") if (ai_note or disclaimer) else ""
    desc = sc.get("description", "").strip()

    # B15: front-load ключевую фразу ПЕРВОЙ строкой YouTube-описания (search/suggested-дискавери).
    # Приоритет: hook → topic + первый keyword ниши → topic. Дубль desc'а не плодим.
    # #13: поля niche.keywords НЕТ ни в одной нише → берём живой дериват из broll_hint/hashtags/topic.
    nd = niche or {}
    first_kw = (nd.get("broll_hint", "") or "").split(",")[0].strip() \
        or next((t.lstrip("#") for t in (nd.get("hashtags") or [])), "") \
        or topic
    key_phrase = (sc.get("hook", "") or "").strip()
    if not key_phrase:
        key_phrase = f"{topic} {first_kw}".strip() if first_kw else topic
    key_phrase = " ".join(key_phrase.split())[:150]      # одна строка, разумная длина
    lead = (key_phrase + "\n\n") if (key_phrase and key_phrase != desc) else ""

    yt_desc = f"{pre}{lead}{desc}\n\n{yt_tags}".strip()
    if lead:
        core.log(f"YouTube-описание: ключевик front-load — «{key_phrase}»", level="info")
    social = f"{pre}{sc.get('caption', '').strip()}\n\n{social_tags}".strip()
    return {
        "youtube": {"title": yt_title, "description": yt_desc},
        "tiktok": {"caption": social[:2100]},
        "instagram": {"caption": social[:2100]},
        "vk": {"caption": f"{pre}{sc.get('caption', '').strip()}\n\n{_ordered('#клипы', raw, 5)}".strip()[:2000]},
    }


def _build_once(niche_id: str, topic: str | None = None, broll_mode: str | None = None,
                serial: dict | None = None, platform: str | None = None) -> dict:
    core.load_local_secrets()
    core.ensure_dirs()
    core.check_disk()                            # анти-«молчаливый ffmpeg-крах» при полном диске
    niche = core.get_niche(niche_id)
    core.set_format(niche.get("format", "short"))   # геометрия всех модулей — от формата ниши
    if niche.get("format") == "long":
        return _build_once_long(niche, topic)
    if broll_mode is None:                       # режим b-roll из ниши (story → ai_images персонажа)
        broll_mode = niche.get("broll_mode", "stock")

    # антиповтор: даём генератору КРОСС-НИШЕВЫЙ список недавних тем (не только этой ниши)
    from pipeline import topics_db
    topics_db.init()
    avoid = topics_db.recent_titles(days=45) or core.recent_topics(niche_id)
    spec = PLATFORM_SPECS.get(platform or "", {})
    sc = scriptmod.generate(niche, topic=topic, avoid=avoid, serial=serial,
                            platform_hint=spec.get("hint", ""), target_words=spec.get("target"))
    if serial:
        sc["_serial_part"] = serial.get("part")     # → _story_caption добавит «продолжение завтра»
    if platform:
        sc["_platform"] = platform
    chunks = scriptmod.to_chunks(sc)
    if not chunks:
        raise RuntimeError("Сценарий пустой — Groq не вернул сегментов")

    out_dir = core.OUTPUT_DIR / f"{core.stamp()}-{niche_id}-{core.slugify(sc['topic'])}"
    work = out_dir / "_work"
    out_dir.mkdir(parents=True, exist_ok=True)

    (out_dir / "script.json").write_text(json.dumps(sc, ensure_ascii=False, indent=2), encoding="utf-8")

    # Тяжёлая часть (озвучка→сток/ИИ→субтитры→рендер→QA) под защитой: при сбое логируем
    # и убираем недособранную папку, чтобы не копился мусор (анти-«зависший» каталог).
    import shutil
    try:
        timed, full_audio, total = voice.synthesize(chunks, sc["voice"], sc["rate"], work,
                                                    engine=niche.get("engine", "edge"), lang=sc["lang"],
                                                    speed=sc.get("speed", 1.0))
        _ensure_disk()                                       # тяжёлый шаг: скачивание сток-клипов
        clips = broll.fetch_for(timed, niche, work, mode=broll_mode, character=sc.get("character", ""))

        palette = niche.get("palette", [["0a0a14", "3ddc97"]])
        accent = palette[0][1] if palette and len(palette[0]) > 1 else "3ddc97"
        sub_mode = niche.get("subtitle_mode", "popin")     # 'popin' (база) | 'karaoke' (\kf, A/B по нишам)
        ass = subtitles.build_ass(timed, out_dir / "subs.ass", accent=accent, mode=sub_mode)
        accents = subtitles.accent_times(timed)            # моменты акцентных слов → punch-in зум фона

        _ensure_disk()                                       # тяжёлый шаг: рендер ffmpeg
        # accents=None → БЕЗ punch-in зума фона (картинка не «прыгает» на жёлтом слове).
        # Акцент теперь только в субтитрах: подсвечиваемое жёлтым слово само увеличивается (popin 122%).
        video = assemble.render(clips, full_audio, total, ass, out_dir / "video.mp4",
                                music_path=_pick_music(), workdir=work, accents=None)
        # рекламный баннер NightFox VPN поверх готового видео (изолированный пост-шаг,
        # не трогает рендер; env VPN_BANNER=0 отключает). До QA → QA проверит итог с баннером.
        try:
            from pipeline import banner
            banner.overlay(video, niche_id=niche_id)   # личный бренд (personal_brand) баннер не получает
        except Exception as e:  # noqa: BLE001
            core.log_error("banner", e, niche=niche_id)
        duration = core.media_duration(video)

        # QA-тестер: тех-косяки (рассинхрон/фриз/разрешение) + AI-зрение (аномалии людей/текста)
        from pipeline import qa as qamod
        qa_result = qamod.check(str(video), workdir=out_dir / "_qa", niche=niche)
    except Exception as e:  # noqa: BLE001
        core.log_error("build._build_once", e, niche=niche_id, dir=out_dir.name)
        shutil.rmtree(out_dir, ignore_errors=True)
        raise
    # Пре-модерация бан-риска: high → складываем в QA-гейт (не публикуем) + сигнал. Дешёвый
    # текстовый проход = страховка аккаунтов (один бан убивает канал). Fail-open в самом ban_risk.
    ban = scriptmod.ban_risk(sc, niche)
    if ban.get("risk") == "high":
        qa_result["ok"] = False
        qa_result["issues"] = list(qa_result.get("issues", [])) + \
            [f"[бан-риск] {ban.get('reason', '')} ({', '.join(ban.get('categories', []))})"]
        core.log(f"Бан-риск HIGH — публикация заблокирована: {ban.get('reason', '')}", level="warn",
                 niche=niche_id, dir=out_dir.name, categories=ban.get("categories", []))

    if not qa_result["ok"]:
        core.log(f"QA не пройден: {qa_result['issues']}", level="warn", niche=niche_id, dir=out_dir.name)

    # дисклеймер: ТОЛЬКО финансовый для «денег» (анти-демонетизация). AI-нота и «художественный
    # вымысел» в подписи БОЛЬШЕ НЕ добавляются (решение владельца — выглядело спамом/палевом).
    # На YouTube «синтетический контент» помечается галкой при загрузке, не текстом.
    disclaimer = ""
    if niche_id == "money_facts" or niche.get("category") == "money":
        disclaimer = "⚠️ Контент носит образовательный характер и не является финансовой рекомендацией."
    # Фаза 3: сюжетная подпись с крючком/вовлечением для соцсетей (VK/IG/Threads). serial_part
    # пробрасывается из meta['serial'] (Фаза 4); сейчас обычная история.
    sc["caption"] = _story_caption(sc, niche, serial_part=sc.get("_serial_part")) or sc.get("caption", "")
    captions = _build_captions(sc, disclaimer=disclaimer, ai_used=False, niche=niche)
    meta = {
        "niche": niche_id,
        "lang": sc["lang"],
        "topic": sc["topic"],
        "duration": round(duration, 2),
        "video": str(video),
        "platforms": niche.get("platforms", []),
        "hashtags": sc.get("hashtags", []),
        "captions": captions,
        "stock_used": sum(1 for c in clips if c["kind"] == "stock"),
        "ai_images": sum(1 for c in clips if c["kind"] == "image"),
        "generated_bg": sum(1 for c in clips if c["kind"] == "gen"),
        "credits": [{"source": c["source"], "url": c.get("source_url", ""), "query": c.get("query", "")}
                    for c in clips if c["kind"] == "stock"],
        "created": core._now().isoformat(),
        "posted": {},
        "qa": qa_result,
        "ban_risk": ban,
        "virality": sc.get("virality", {}),
        "hook": sc.get("hook", ""),                      # для обложки (короткий хук-текст)
        "thumb_text": sc.get("thumb_text", ""),          # приоритетный текст обложки (законченная фраза)
        "hook_variants": sc.get("hook_variants", []),
        "title_variants": sc.get("title_variants", []),
    }

    # авто-обложка с ВЫБОРОМ по кликабельности (Gemini-зрение): несколько вариантов → лучший +
    # Vision-QA читаемости. Фолбэк на один вариант, если зрение недоступно. Не критично для видео.
    try:
        from pipeline import thumbnail
        thumb = thumbnail.make_best_for_meta(str(video), meta, out_dir / "thumb.jpg")
        if thumb:
            meta["thumbnail"] = str(thumb)
        tq = meta.get("thumb_qa") or {}
        if tq.get("readable") is False:                  # обложка нечитаема — сигнал, но НЕ блок публикации
            core.log(f"обложка нечитаема (Vision-QA): {tq.get('issue', '')}", level="warn",
                     niche=niche_id, dir=out_dir.name)
    except Exception as e:  # noqa: BLE001
        core.log_error("thumbnail", e)

    (out_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    # CREDITS.txt — доказательство лицензий для возможных споров по Content ID
    if meta["credits"]:
        lines = ["Источники B-roll (лицензии Pexels/Pixabay — коммерческое использование без обязательной атрибуции):", ""]
        for c in meta["credits"]:
            lines.append(f"- {c['source']}: {c['url']}  (запрос: {c['query']})")
        (out_dir / "CREDITS.txt").write_text("\n".join(lines), encoding="utf-8")

    post_txt = [
        f"# {sc['topic']}  ({duration:.1f}s · {niche_id})", "",
        "── YouTube Shorts ──", captions["youtube"]["title"], "", captions["youtube"]["description"], "",
        "── TikTok ──", captions["tiktok"]["caption"], "",
        "── Instagram Reels ──", captions["instagram"]["caption"], "",
        "── VK ──", captions["vk"]["caption"], "",
    ]
    (out_dir / "POST.txt").write_text("\n".join(post_txt), encoding="utf-8")

    core.append_history({
        "niche": niche_id, "topic": sc["topic"], "lang": sc["lang"],
        "duration": round(duration, 2), "dir": str(out_dir), "status": "built",
    })

    # материалы (ИИ-картинки/использованные кадры) → media_assets/<slug>/ (сохраняем для повторного монтажа)
    import shutil
    slug = out_dir.name
    media_sub = core.MEDIA_DIR / slug
    media_sub.mkdir(parents=True, exist_ok=True)
    for img in work.glob("img_*.png"):                       # ИИ-картинки персонажа/сцен
        shutil.copy2(img, media_sub / img.name)
    # копия сценария рядом с материалами — что озвучивали под эти кадры
    shutil.copy2(out_dir / "script.json", media_sub / "script.json")

    # готовое видео для публикации (только если QA прошёл) → publish/
    publish_path = ""
    if qa_result["ok"]:
        publish_path = str(core.PUBLISH_DIR / f"{slug}.mp4")
        shutil.copy2(video, publish_path)

    # чистим промежуточные файлы (клипы/склейки/озвучка ~8 МБ на ролик) — оставляем только результат
    shutil.rmtree(work, ignore_errors=True)
    shutil.rmtree(out_dir / "_qa", ignore_errors=True)

    return {"dir": str(out_dir), "video": str(video), "publish": publish_path,
            "duration": duration, "meta": meta, "script": sc, "qa": qa_result}


def _build_once_long(niche: dict, topic: str | None) -> dict:
    """Сборка длинной документалки 16:9: сценарий по главам (script_long) → озвучка → b-roll →
    рендер БЕЗ вжигания субтитров → SRT → QA → обложка 1280×720. Контракт возврата тот же,
    что у _build_once — retry/форс-логика build_video работает без изменений."""
    niche_id = niche["id"]
    from pipeline import topics_db, script_long
    topics_db.init()
    avoid = topics_db.recent_titles(days=60)
    if not topic:
        from pipeline import selector
        picked = selector.pick_topics(niche, 1, recent=avoid)
        topic = (picked or [""])[0]
        if not topic:
            raise RuntimeError("selector не дал тему для long-form")
    sc = script_long.generate_long(niche, topic, avoid=avoid)
    chunks = script_long.to_chunks(sc)
    if not chunks:
        raise RuntimeError("Длинный сценарий пуст")

    out_dir = core.OUTPUT_DIR / f"{core.stamp()}-{niche_id}-{core.slugify(sc['topic'])}"
    work = out_dir / "_work"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "script.json").write_text(json.dumps(sc, ensure_ascii=False, indent=2), encoding="utf-8")

    import shutil
    try:
        timed, full_audio, total = voice.synthesize(chunks, sc["voice"], sc["rate"], work,
                                                    engine=niche.get("engine", "kokoro"),
                                                    lang=sc["lang"], speed=sc.get("speed", 1.0))
        _ensure_disk()                                       # тяжёлый шаг: скачивание сток-клипов
        clips = broll.fetch_for(timed, niche, work, mode=niche.get("broll_mode", "mixed"))
        _ensure_disk()                                       # тяжёлый шаг: рендер ffmpeg
        video = assemble.render(clips, full_audio, total, None, out_dir / "video.mp4",
                                music_path=_pick_music(), workdir=work, accents=None, loop=False)
        srt = subtitles.build_srt(timed, out_dir / "subs.srt")
        duration = core.media_duration(video)
        from pipeline import qa as qamod
        qa_result = qamod.check(str(video), workdir=out_dir / "_qa", niche=niche)
    except Exception as e:  # noqa: BLE001
        core.log_error("build._build_once_long", e, niche=niche_id, dir=out_dir.name)
        shutil.rmtree(out_dir, ignore_errors=True)
        raise

    # бан-риск: ban_risk читает segments — подставляем главы тем же полем
    ban = scriptmod.ban_risk({**sc, "segments": [{"text": c["text"]} for c in sc.get("chapters", [])]},
                             niche)
    if ban.get("risk") == "high":
        qa_result["ok"] = False
        qa_result["issues"] = list(qa_result.get("issues", [])) + \
            [f"[бан-риск] {ban.get('reason', '')} ({', '.join(ban.get('categories', []))})"]

    # таймкоды глав: старт первого body-чанка каждой главы → "MM:SS Заголовок" в описании
    ch_starts: dict = {}
    for t in timed:
        if t.get("role") == "body" and "chapter" in t and t["chapter"] not in ch_starts:
            ch_starts[t["chapter"]] = t.get("start", 0.0)

    def _mmss(s: float) -> str:
        s = int(s)
        return f"{s // 60:02d}:{s % 60:02d}"

    chapter_lines = ["00:00 Introduction"] + [
        f"{_mmss(ch_starts.get(i, 0))} {c['heading']}" for i, c in enumerate(sc.get("chapters", []))]
    description = (sc.get("description", "") or "").replace("{CHAPTERS}", "\n".join(chapter_lines))

    ai_images = sum(1 for c in clips if c["kind"] == "image")
    meta = {
        "niche": niche_id,
        "lang": sc["lang"],
        "format": "long",
        "topic": sc["topic"],
        "title": sc.get("title", sc["topic"]),
        "description": description,
        "tags": sc.get("tags", []),
        "duration": round(duration, 2),
        "video": str(video),
        "srt": str(srt),
        "chapters": chapter_lines,
        "platforms": niche.get("platforms", ["youtube"]),
        "stock_used": sum(1 for c in clips if c["kind"] == "stock"),
        "ai_images": ai_images,
        "generated_bg": sum(1 for c in clips if c["kind"] == "gen"),
        # фотореалистичные AI-кадры в кадре → при загрузке нужна галка YouTube "altered content"
        "ai_disclosure": ai_images > 0,
        "credits": [{"source": c["source"], "url": c.get("source_url", ""), "query": c.get("query", "")}
                    for c in clips if c["kind"] == "stock"],
        "created": core._now().isoformat(),
        "posted": {},
        "qa": qa_result,
        "ban_risk": ban,
        "virality": {"score": (sc.get("quality") or {}).get("score", 0)},   # совместимость с _commit
        "quality": sc.get("quality", {}),
        "hook": sc.get("hook", ""),
        "thumb_text": sc.get("thumb_text", ""),
        "sources": sc.get("sources", []),
    }

    try:
        from pipeline import thumbnail
        thumb = thumbnail.make_best_for_meta(str(video), meta, out_dir / "thumb.jpg")
        if thumb:
            meta["thumbnail"] = str(thumb)
    except Exception as e:  # noqa: BLE001
        core.log_error("thumbnail", e)

    (out_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    if meta["credits"]:
        lines = ["Источники B-roll (лицензии Pexels/Pixabay — коммерческое использование без обязательной атрибуции):", ""]
        for c in meta["credits"]:
            lines.append(f"- {c['source']}: {c['url']}  (запрос: {c['query']})")
        (out_dir / "CREDITS.txt").write_text("\n".join(lines), encoding="utf-8")

    # POST.txt — всё для ручной выкладки в Studio (мост до прохождения API-аудита Google)
    post_txt = [
        f"# {meta['title']}  ({duration / 60:.1f} мин · {niche_id})", "",
        f"AI-disclosure (галка Altered content): {'ДА' if meta['ai_disclosure'] else 'нет'}", "",
        "── Title ──", meta["title"], "",
        "── Description ──", description, "",
        "── Tags ──", ", ".join(meta["tags"]), "",
    ]
    (out_dir / "POST.txt").write_text("\n".join(post_txt), encoding="utf-8")

    core.append_history({
        "niche": niche_id, "topic": sc["topic"], "lang": sc["lang"],
        "duration": round(duration, 2), "dir": str(out_dir), "status": "built",
    })

    slug = out_dir.name
    media_sub = core.MEDIA_DIR / slug
    media_sub.mkdir(parents=True, exist_ok=True)
    for img in work.glob("img_*.png"):
        shutil.copy2(img, media_sub / img.name)
    shutil.copy2(out_dir / "script.json", media_sub / "script.json")

    publish_path = ""
    if qa_result["ok"]:
        publish_path = str(core.PUBLISH_DIR / f"{slug}.mp4")
        shutil.copy2(video, publish_path)

    shutil.rmtree(work, ignore_errors=True)
    shutil.rmtree(out_dir / "_qa", ignore_errors=True)

    return {"dir": str(out_dir), "video": str(video), "publish": publish_path,
            "duration": duration, "meta": meta, "script": sc, "qa": qa_result}


def build_video(niche_id: str, topic: str | None = None, broll_mode: str | None = None,
                max_attempts: int = 2, serial: dict | None = None, platform: str | None = None) -> dict:
    """Собрать ролик с авто-регенерацией: если QA не прошёл — пересобрать (до max_attempts).
    Если за все попытки визуальный QA так и не пройден, а брак ЧИСТО визуальный (AI-артефакты:
    техника ок И бан-риск не high) — публикуем ЛУЧШИЙ из попыток (минимум дефектов), а не бросаем
    нишу (решение владельца: «за 2 попытки не вышло — ставь лучший и позуй»). Технический брак
    (нет звука/разрешение/фриз) и бан-риск НЕ форсим никогда. serial — эпизод; platform — формат."""
    import shutil
    core.cleanup_cache(max_age_days=7)          # подчищаем старый скачанный сток (анти-рост диска)
    core.cleanup_outputs(max_age_days=21)        # старые папки роликов/публикаций (анти-рост диска)
    core.cleanup_media(max_age_days=45)          # старые материалы для повторного монтажа (анти-рост диска)

    def _release(r):
        # освобождаем зарезервированную тему бракованной попытки — иначе ретрай/завтра словит
        # СВОЙ же резерв как ложный дубль и не сможет взять тему
        try:
            from pipeline import topics_db
            topics_db.release_topic(niche_id, r.get("meta", {}).get("topic", "")
                                    or r.get("script", {}).get("topic", ""))
        except Exception as e:  # noqa: BLE001
            core.log_error("topics_db.release_topic", e)

    def _commit(r):
        # тему ОПУБЛИКОВАННОГО ролика фиксируем как built (антиповтор в будущем)
        try:
            from pipeline import topics_db
            m = r["meta"]
            topics_db.commit_topic(niche_id, m["topic"], lang=m.get("lang", "ru"),
                                   dir=pathlib.Path(r["dir"]).name,
                                   hook=r.get("script", {}).get("hook", ""),
                                   virality=(m.get("virality", {}) or {}).get("score", 0))
        except Exception as e:  # noqa: BLE001
            core.log_error("topics_db.commit_topic", e)

    def _visual_only(r):
        # брак ТОЛЬКО визуальный: техника ок И бан-риск не high → вариант можно форс-публиковать
        q = r.get("qa", {})
        tech_ok = (q.get("technical") or {}).get("ok", True)
        ban_high = (r.get("meta", {}).get("ban_risk") or {}).get("risk") == "high"
        return tech_ok and not ban_high

    best = best_score = last = None
    for attempt in range(max_attempts):
        res = _build_once(niche_id, topic=topic, broll_mode=broll_mode, serial=serial, platform=platform)
        if res["qa"]["ok"]:
            _commit(res)
            core.log(f"Готов ролик: {res['meta']['topic']}", niche=niche_id,
                     virality=res["meta"].get("virality", {}).get("score"),
                     attempt=attempt + 1, dir=pathlib.Path(res["dir"]).name)
            for stale in (best, last):           # отложенные кандидаты (форс/тех-брак) больше не нужны:
                if stale is not None:            # чистим ОБА, иначе утечка резерва темы + осиротевшая папка
                    shutil.rmtree(stale["dir"], ignore_errors=True); _release(stale)
            return res
        if _visual_only(res):                    # держим ЛУЧШИЙ (меньше визуальных дефектов) кандидат
            score = len(((res["qa"].get("visual") or {}).get("issues")) or [])
            if best is None or score < best_score:
                if best is not None:
                    shutil.rmtree(best["dir"], ignore_errors=True); _release(best)
                best, best_score = res, score
            else:
                shutil.rmtree(res["dir"], ignore_errors=True); _release(res)
        else:                                    # тех-брак/бан-риск — такой вариант не публикуем
            if last is not None:
                shutil.rmtree(last["dir"], ignore_errors=True); _release(last)
            last = res
    # QA не прошёл ни разу. Приоритет — форс-публикация лучшего «только визуальный брак».
    if best is not None:
        best["qa"]["ok"] = True                  # разрешаем публикацию (autopilot гейтит по qa.ok)
        best["qa"]["forced"] = True              # честно помечаем: опубликовано несмотря на артефакты
        _commit(best)
        core.log(f"Форс-публикация лучшего из {max_attempts} (визуальные артефакты не исправлены): "
                 f"{best['meta']['topic']}", level="warn", niche=niche_id,
                 issues=best["qa"].get("issues"), dir=pathlib.Path(best["dir"]).name)
        if last is not None:
            shutil.rmtree(last["dir"], ignore_errors=True); _release(last)
        return best
    # форсить нечего (тех-брак/бан-риск во всех попытках) → не публикуем, как раньше
    if last is not None:
        _release(last)
        core.log(f"Ролик отдан с непройденным QA (тех/бан — не форсим): {last['meta']['topic']}",
                 level="warn", niche=niche_id, issues=last["qa"]["issues"],
                 dir=pathlib.Path(last["dir"]).name)
    return last


if __name__ == "__main__":
    nid = sys.argv[1] if len(sys.argv) > 1 else "ai_lifehacks"
    topic = sys.argv[2] if len(sys.argv) > 2 else None
    res = build_video(nid, topic)
    print(f"\n✅ Готово: {res['video']}\n   {res['duration']:.1f}s · {res['dir']}")
