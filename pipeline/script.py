"""Генерация сценария короткого видео через Groq → строгий JSON.

Один вызов отдаёт всё нужное для ролика: хук, 4-6 сегментов (каждый со своим
англоязычным запросом для стокового B-roll), аутро-CTA и метаданные под площадки
(title/description/hashtags/caption). Англоязычные broll_query — потому что стоковые
библиотеки (Pexels/Pixabay) индексируются по-английски.

Зависимостей нет — Groq через urllib (как в content-engine).
"""
import os
import re
import json
import urllib.request
import urllib.error

import sys as _sys
import pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))
import core as _core  # noqa: E402 — модуль-уровневый алиас (нужен в _three_hooks/_title_variants)

MODEL = "llama-3.3-70b-versatile"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

ANTI_SLOP_EN = (
    "WRITE LIKE A REAL PERSON, not a robot narrator. BANNED: clichés ('in today's world', 'it's no secret', "
    "'let's dive in', 'in this video'), filler, long intros. Plain English only. "
    "DO NOT invent specific stats/numbers — if unsure, give the mechanism/principle, not a fabricated figure. "
    "DO: short punchy sentences, conversational 'you' tone, concrete examples, action verbs."
)

# Виральный плейбук 2026 (из ресёрча топ-шортсов): хук-формулы, структура удержания, бан-лист, углы по нишам.
HOOKS_EN = (
    "The HOOK (first 1.5s) decides everything. Build it on ONE of the top-2026 formulas (the technique, not a copy):\n"
    "  • Identity call: 'If you [role/situation] — [unexpected promise].'\n"
    "  • Contrarian: 'Stop [common action]. Here's why it kills your results.'\n"
    "  • Specific number: 'I [did X] [N times] — here are the [M] that actually worked' (exact, odd numbers).\n"
    "  • Confession: 'I lost [amount/time] on [action] before I learned one thing.'\n"
    "  • Proof-first: '[Concrete result in a timeframe]. Here's exactly how.'\n"
    "  • Mistake/loss: 'Your [thing] [fails] not because of [obvious] — [hidden real reason].'\n"
    "  • Myth-bust: 'Everyone thinks [belief]. Actually [truth].'\n"
    "  • Free replaces paid: 'This free [thing] replaces [paid/$] — almost nobody knows.'\n"
    "RULES: ≤14 words; stack 2 triggers (curiosity+self-relevance OR loss+proof); OPEN a loop — promise the payoff, don't give it in the hook.\n"
    "The FIRST WORD of the hook must be charged (number/name/action verb/shock noun); "
    "NEVER start with filler: so/well/you know/okay/basically/imagine/ever."
)

STRUCT_EN = (
    "RETENTION STRUCTURE (loops):\n"
    "  1) Hook opens the MAIN loop (promise of a payoff).\n"
    "  2) Each segment closes one micro-question and immediately opens the next ('but here's where everyone slips'). Stack 2-3 micro-loops.\n"
    "  3) Hold the MAIN payoff (name/answer/insight) until the LAST segment or outro (~80-90% in), not earlier.\n"
    "  4) Segment 1 is NOT setup: it immediately raises the stakes or gives the first concrete detail/number/turn, "
    "partially answering the hook's intrigue while opening the next loop.\n"
    "  5) 1 video = 1 idea = 1 payoff. Concrete, numbers, not theory. Plant a moment worth sharing with a friend."
)

# Сторителлинг-плейбук (для ниш с format:"story") — хук→нагнетание→твист→петля.
STORY_HOOKS_EN = (
    "STORY HOOK (first 1.5s) — open an intrigue LOOP the viewer must resolve:\n"
    "  • '[Name/object] did [the impossible]. Here's what happened next.'\n"
    "  • 'Nobody knows what really happened to [X].'\n"
    "  • 'In [year], one 30-second decision changed [everything].'\n"
    "  • 'Everyone thought [obvious]… they were wrong.'\n"
    "RULES: ≤14 words; informative first word; defy expectations; don't reveal the payoff in the hook."
)
STORY_STRUCT_EN = (
    "STORY STRUCTURE (narrative arc, not a fact list):\n"
    "  1) Hook opens the main loop.\n"
    "  2) ESCALATE 'but → therefore → but'; mid-way internal re-hook ('and that's when it all went wrong…'). Numbers/dates as anchors.\n"
    "  3) TWIST near the end that REFRAMES what was said (reversal/scale/irony), not just more info.\n"
    "  4) Segment 1 is NOT setup: it immediately raises the stakes or gives the first concrete detail/number/turn, "
    "partially answering the hook's intrigue while opening the next loop.\n"
    "  5) LOOP ending: question to comments OR callback to the hook. 1 video = 1 story = 1 twist."
)

AVOID_EN = (
    "BAN (instant skip / algo penalty): generic intros ('hey guys', 'in this video', 'today we'll talk'); "
    "the worn-out 'nobody talks about'; fake urgency ('watch before it's deleted'); slow story starts; "
    "single-trigger hooks; clickbait with no payoff; 'like and subscribe'; payoff at 100% (end) or 50% (middle); "
    "top-N with no narrative; the most burnt-out niche topics without a fresh angle; filler words."
)

# Виральные углы по категориям ниш (копируемые приёмы; под выбранный угол придумывается тема)
ANGLES = {
    "ai": [
        "Free replaces paid: 'This free AI tool does what people pay $50/mo for' (hold the name until the end)",
        "Result first: 'This AI did [a week of work] in [N minutes]' — demo/screencast energy",
        "Tool stack, #1 held for last: '3 AI tools almost nobody combines'",
        "'You're prompting it wrong' — before/after format",
        "Tested-by-number: 'I tested 23 AI tools, 20 are junk — keep these 3'",
        "Hidden features: 'Hidden [service] features 90% of users never find'",
    ],
    "psychology": [
        "'You've been lied to': 'You're not lazy — here's what procrastination actually is'",
        "Dark psychology: 'This is being used on you right now' (gaslighting, love bombing) — high share rate",
        "Self-identification test: '5 signs you're a [type]' — drives saves and tags",
        "Famous experiment told as a thriller: open mid-action → twist → bridge to today",
        "Body/brain: 'Your brain dumps cortisol every time you [action]'",
    ],
    "money": [
        "Compound-interest shock: '$100 a month from age 20 = [X] by retirement' — one number",
        "Contrarian authority: 'Why I will never put money into [popular thing]'",
        "Rich vs broke, no preaching: 'Broke people borrow for lifestyle, rich people borrow for assets'",
        "Opportunity cost: 'Cash in a 0.01% savings account with 8% inflation — you are losing money'",
        "Auto-system: 'I set one automatic savings rule and forgot about it' — drives saves",
    ],
    "history": [
        "Myth-bust: 'Everyone learned [school myth]. Here's what actually happened'",
        "'They don't teach this in school' — position the viewer as misled by institutions",
        "One person's story: 'In [year], one [role] [prevented/changed]...'",
        "Ancient↔modern mirror: 'Rome did exactly what [we do] today'",
        "What-if: 'What if [event] had gone the other way?' — drives comment debates",
    ],
    "talking_objects": [
        "The object speaks in FIRST person with attitude: 'I'm your credit card, and I'm done staying silent'",
        "Object as witness: 'I'm the elevator mirror. You wouldn't believe what I've seen'",
        "Abandoned object: 'Nobody has opened me in 3 months. Here's what I learned about my owner'",
        "The object judges its owner with humor/sarcasm, twist at the end — an unexpected moral",
    ],
    "business": [
        "Collapse: 'How [company] lost everything over one decision'",
        "All-in: 'He bet his last money on [X]. Here's how it went'",
        "One mistake worth a billion: unexpected turn at the end",
        "Brand origin: 'What [famous brand] really started as' — myths vs facts",
        "Rich vs everyone: the counterintuitive decision that changed the game",
    ],
    "mystic": [
        "Urban legend with a real basis: 'Where the belief about [X] actually comes from'",
        "The unexplained, told first-person by a (fictional) witness: escalation → twist",
        "Abandoned place/object with a backstory: 'What they found inside [place]'",
        "Twist-reversal: what looked supernatural turned out to be [unexpected]",
    ],
    "whatif": [
        "History fork: 'What if [event] had gone differently' → chain of consequences",
        "A great figure's personal fork: one different decision → a different world",
        "Mirror to today: 'and here is what today would look like'",
    ],
    "engineering": [
        "Impossible build: 'They said it couldn't be built. [N] workers proved them wrong'",
        "Hidden mechanism: 'The invisible trick that keeps [structure] standing'",
        "Disaster lesson: 'One bolt/valve/decimal point brought down [project]'",
        "Scale shock: '[Structure] used enough [material] to [absurd comparison]'",
    ],
    "psy_story": [
        "One person's story: 'For 3 years [hero] couldn't figure out why [problem]. Then this surfaced'",
        "Recognition: a hyper-specific symptom the viewer has felt → the phenomenon explained",
        "Dark relationship pattern told as a story (narcissist/gaslighting) → a protective insight",
    ],
}
# Основной путь — поле category в niches.json; карта ниже — фолбэк по id ниши.
_NICHE_CATEGORY: dict[str, str] = {}


def _angles_for(niche: dict) -> list[str]:
    cat = niche.get("category") or _NICHE_CATEGORY.get(niche.get("id", ""))
    return ANGLES.get(cat, [])


def _groq(system: str, user: str, temp: float = 0.85, max_tokens: int = 900,
          json_mode: bool = True) -> str:
    """Текстовая генерация через мульти-провайдерный каскад (Groq→Cerebras→Gemini→…).
    Имя оставлено для совместимости вызовов; фолбэк живёт в pipeline/llm.py — если у одного
    провайдера кончились бесплатные токены (429), запрос идёт к следующему, система не падает."""
    from pipeline import llm
    return llm.chat(system, user, json_mode=json_mode, max_tokens=max_tokens, temp=temp)


def _clean_line(s: str) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip().strip('"«»')


def _word_count(sc: dict) -> int:
    return len((sc.get("hook", "") + " " + " ".join(s.get("text", "") for s in sc.get("segments", []))
                + " " + sc.get("outro", "")).split())


def _trim_to_words(sc: dict, max_words: int = 80, min_segments: int = 4) -> dict:
    """Механическая гарантия длины: режем лишние сегменты (с конца, outro-выплата остаётся),
    пока озвучка > max_words, но не ниже min_segments. ~2.3 слова/сек → 80 слов ≈ 33-35 сек."""
    while _word_count(sc) > max_words and len(sc.get("segments", [])) > min_segments:
        sc["segments"].pop()                 # последний body-сегмент; хук и outro трогать нельзя
    # перепривязать хвостовой broll-запрос (на случай если outro ссылался на удалённый сегмент)
    if sc.get("segments"):
        sc["_hook_query"] = sc.get("_hook_query") or sc["segments"][0].get("broll_query", "")
    return sc


def generate(niche: dict, topic: str | None = None, avoid: list[str] | None = None,
             serial: dict | None = None, platform_hint: str = "", target_words: int | None = None) -> dict:
    """Сгенерировать сценарий под нишу. serial={'part':1} → завязка+клиффхэнгер (часть 1 из 2);
    serial={'part':2,'premise':...} → продолжение. platform_hint — стиль/формат под площадку
    (YouTube/TikTok/Reels-VK). target_words — целевая длина озвучки под площадку (в окне validate 62-92).
    Возвращает нормализованный dict."""
    import core as _core
    # целевая длина под площадку, зажатая в окно validate() (62-92): диапазон для промпта и trim
    tw = int(target_words) if target_words else 80
    tw = max(66, min(90, tw))
    w_lo, w_hi = max(62, tw - 6), min(92, tw + 5)
    w_trim = min(92, tw + 8)
    if topic:                                  # анти-prompt-injection: тема приходит из любых источников
        topic = _core.sanitize_external(topic)
    lang = niche.get("lang", "en")
    anti = ANTI_SLOP_EN
    hooks = HOOKS_EN
    struct = STRUCT_EN
    if niche.get("format") == "story":   # сторителлинг-плейбук вместо «фактов»
        hooks = STORY_HOOKS_EN
        struct = STORY_STRUCT_EN
    avoid_block = AVOID_EN   # бан-лист приёмов для system-промпта
    angles = _angles_for(niche)
    angles_block = ""
    if angles:
        head = "VIRAL ANGLES for this niche (pick ONE, invent a topic for it):"
        angles_block = head + "\n" + "\n".join(f"  • {a}" for a in angles) + "\n\n"
    lang_rule = "Write ALL spoken text (hook/segments/outro) in ENGLISH only. "

    avoid_line = ""
    if avoid:
        snip = " | ".join(_clean_line(t)[:70] for t in avoid[:12] if t)
        if snip:
            avoid_line = "\nDO NOT repeat recent topics (pick a different one): " + snip

    topic_line = ""
    if topic:
        topic_line = f"\nSPECIFIC TOPIC for this video: {topic}"

    # СЕРИЙНЫЙ контент: часть 1 (завязка+клиффхэнгер) / часть 2 (продолжение+развязка по premise).
    serial_line = ""
    if serial and serial.get("part") == 1:
        serial_line = ("\n📺 PART 1 of a 2-part serial: tell the FIRST half — setup and rising tension to a "
                       "peak, but DO NOT reveal the resolution (it comes in part 2). End on a cliffhanger.")
    elif serial and serial.get("part") == 2:
        prem = _core.sanitize_external(str(serial.get("premise", "")))[:300]
        serial_line = (f"\n📺 PART 2 (finale). Part 1 was: '{prem}'. Continue the SAME story, briefly recap "
                       "in the hook, and resolve it.")

    # СИД ИЗ HEATMAP: пик удержания у успешного конкурента (yt-dlp Most Replayed) как затравка приёма.
    # Опционально (CF_HEATMAP=0 выключает), сетевой вызов — мягкий, не роняет генерацию.
    seed_line = ""
    if os.environ.get("CF_HEATMAP", "1") != "0" and not topic:
        try:
            from pipeline import heatmap
            kw = (niche.get("broll_hint", "") or niche.get("title", "")).split(",")[0].strip()
            seed = heatmap.hook_seed(kw, lang=lang) if kw else None
            ref = (seed or {}).get("peak_label") or (seed or {}).get("title")
            ref = _core.sanitize_external(ref) if ref else ref   # чужой текст из heatmap → чистим перед вставкой в промпт
            if ref:
                seed_line = (f"\nRETENTION SIGNAL: a top competitor's attention peak was at '{ref[:90]}' — "
                             f"use a similar angle as INSPIRATION (do not copy).")
        except Exception:  # noqa: BLE001 — сид необязателен
            pass

    # ВИРАЛЬНЫЕ СИГНАЛЫ НИШИ (ЛЕГАЛЬНЫЙ реверс-инжиниринг чужих топ-Shorts: метаданные+субтитры).
    # Богатый бриф (горячие подтемы / хук-паттерны) поверх точечного seed_line. Гейт CF_VIRAL
    # (дефолт вкл), при пустом брифе ведём себя как раньше — модуль НИКОГДА не роняет генерацию.
    if os.environ.get("CF_VIRAL", "1") != "0":
        try:
            from pipeline import heatmap
            vq = (niche.get("broll_hint", "") or niche.get("title", "")).split(",")[0].strip()
            brief = heatmap.viral_brief(vq, lang=lang) if vq else {}
            subs = [_core.sanitize_external(s) for s in (brief.get("hot_subtopics") or [])[:8]]
            subs = [s for s in subs if s]
            pats = [_core.sanitize_external(p) for p in (brief.get("hook_patterns") or [])[:6]]
            pats = [p for p in pats if p]
            if subs or pats:
                seed_line += "\nVIRAL SIGNALS FOR THIS NICHE (for inspiration, do NOT copy verbatim):"
                if subs:
                    seed_line += " hot subtopics — " + ", ".join(subs) + ";"
                if pats:
                    seed_line += " hook patterns — " + " | ".join(pats) + "."
        except Exception:  # noqa: BLE001 — бриф необязателен
            pass

    # КАЛЕНДАРНЫЙ ХУК: если рядом инфоповод под нишу (НГ/8марта/Чёрная пятница/1сент…) — подмешиваем
    # угол (контент за 2-4 дня ДО собирает больше). Только когда тема не задана. Гейт CF_CALENDAR.
    if os.environ.get("CF_CALENDAR", "1") != "0" and not topic:
        try:
            from pipeline import calendar_hooks
            cal = calendar_hooks.angle_for(niche.get("id", ""), _core.today_str())
            if cal:
                seed_line += f"\n📅 TIMELY OCCASION (weave in naturally if it fits): {cal}."
        except Exception:  # noqa: BLE001 — календарь необязателен
            pass

    is_story = niche.get("format") == "story"
    char_schema = ('  "character": "ENGLISH visual description of the MAIN subject/character to show in EVERY shot '
                   '(e.g. \'a worn teal-blue credit card with a gold chip\'); keep it the SAME across the video",\n'
                   if is_story else "")
    schema_hint = (
        '{\n'
        '  "topic": "short video topic (3-6 words)",\n'
        + char_schema +
        '  "hook": "opening hook line, 1 sentence, grabs within 2 seconds",\n'
        '  "segments": [\n'
        '    {"text": "1-2 short sentences of substance", "broll_query": "english scene description for this exact line"}\n'
        '  ],\n'
        '  "outro": "final line + soft subscribe nudge",\n'
        '  "thumb_text": "2-4 words for the THUMBNAIL: the most gripping essence in big text, a COMPLETE '
        "phrase (never cut off on a preposition/conjunction), e.g. 'You lose money' or 'Your brain lies'\",\n"
        '  "title": "YouTube title up to 80 chars, clickable",\n'
        '  "description": "2-3 lines of YouTube description",\n'
        '  "caption": "short caption for TikTok/Instagram, 1-2 lines",\n'
        '  "hashtags": ["#tag1", "#tag2", "..."]\n'
        '}'
    )

    system = (
        f"You are a scriptwriter of VIRAL vertical videos (Shorts/TikTok/Reels) for a faceless channel. "
        f"Your videos grab attention within 1.5 seconds and hold it to the end.\n"
        f"NICHE: {niche.get('title')} — {niche.get('topic_brief')}\n"
        f"TONE: {niche.get('tone')}\n{lang_rule}\n\n"
        f"{hooks}\n\n{struct}\n\n{angles_block}"
        f"JSON OUTPUT FORMAT: hook + 3-5 segments + outro.\n"
        f"  • hook — ≤14 words using one of the formulas above, stacking 2 triggers; opens a loop. "
        f"NO 'hey guys / in this video / today'.\n"
        f"  • TRUTH RULE: every factual claim in hook/title/segments must be TRUE and verifiable. "
        f"NEVER invent bans, laws, deaths or 'illegal today' angles for drama — fabricated claims "
        f"kill the channel (YouTube inauthentic-content policy).\n"
        f"  • segments — 4-6 items, 1-2 sentences each; every segment closes one micro-question and OPENS the next. "
        f"Do NOT reveal the main payoff/name before the last segment. If the total runs under 70 words, "
        f"add another segment: the video must not be shorter than ~30 seconds.\n"
        f"  • outro — the PAYOFF (main insight/name) + PREFERABLY close the LOOP: the final line "
        f"echoes the hook (a callback with the same words) so the video loops seamlessly — that earns "
        f"+30% distribution (every replay = a view). CTA — a short question for the comments or 'save this' "
        f"(in the spirit of '{niche.get('cta')}'), NEVER 'like and subscribe'.\n"
        f"  • Total spoken text STRICTLY {w_lo}-{w_hi} words (video ~{int(w_lo/2.3)}-{int(w_hi/2.3)} seconds). Dense, no filler, don't pad.\n"
        f"  • broll_query — a CONCRETE visual scene that PRECISELY illustrates THIS exact line "
        f"(not the video's general topic — literally what is on screen while these words play). "
        + (f"THIS IS A STORY ABOUT ONE CHARACTER: EVERY broll_query must show the same character from the "
           f"\"character\" field in this line's action/situation (e.g. 'the credit card lying forgotten in a dark wallet', "
           f"'the credit card swiped at a terminal, sparks'). 4-8 English words, cinematic.\n"
           if is_story else
           f"4-6 English words, a real scene with people/objects matching the meaning (e.g. a line about money → "
           f"'man counting cash at kitchen table'; about sleep → 'exhausted woman lying awake in bed'; about doors/memory → "
           f"'person pausing confused in a doorway'). VARY the shots, don't repeat people/backgrounds.\n") +
        f"  STRICTLY FORBIDDEN: abstract, 3d render, patterns, particles, motion graphics, logo, badge, icon, neon shapes. "
        f"Niche reference: {niche.get('broll_hint')}. Avoid stock clichés (handshake, lightbulb, typing laptop).\n\n"
        f"{anti}\n\n{avoid_block}\n\n"
        f"Return STRICTLY valid JSON per the schema (no markdown, no comments):\n{schema_hint}"
    )
    platform_line = (("\n🎯 " + platform_hint) if platform_hint else "")
    user = f"Generate one short-video script for the niche '{niche.get('title')}'.{topic_line}{platform_line}{serial_line}{seed_line}{avoid_line}"

    # Генерация с ДВОЙНЫМ ГЕЙТОМ + САМО-УЛУЧШЕНИЕМ: validate() + Virality Score (LLM-судья).
    # Цикл: сгенерь → оцени → если ниже планки, скорми судейский «fix» и слабую ось обратно
    # модели и перепиши → оцени снова. До MAX_SCRIPT_ATTEMPTS. Возвращаем первый, взявший планку,
    # иначе — лучший набранный балл.
    best = None
    fallback = None        # любой непустой сценарий — страховка от пустого результата
    feedback = ""
    niche_id = niche.get("id", "")
    held_topic = ""        # тема, которую СЕЙЧАС держим зарезервированной (та, что в best); чужие резервы освобождаем

    def _release(topic: str) -> None:    # освободить наш резерв (брак/замена best), чтобы не блокировать тему
        if not topic:
            return
        try:
            from pipeline import topics_db
            topics_db.release_topic(niche_id, topic)
        except Exception:  # noqa: BLE001
            pass

    for attempt in range(MAX_SCRIPT_ATTEMPTS):
        try:
            sc = _normalize(_parse(_groq(system, user + feedback)), niche)
            try:
                sc = _polish(sc, niche)
            except Exception:  # noqa: BLE001 — редактор необязателен
                pass
        except Exception:  # noqa: BLE001 — сбой LLM/парса, пробуем ещё раз
            continue
        sc = _trim_to_words(sc, max_words=w_trim)   # механически режем до целевой длины под площадку
        if sc.get("segments"):
            fallback = fallback or sc
        if not validate(sc)[0]:
            continue
        # S4(1) — жёсткий гейт петли: outro должен дословно перекликаться с хуком (≥2 общих значимых слова),
        # тогда replay бесшовен. Дешёвая механическая проверка (без LLM). Не принимаем такой вариант —
        # докручиваем feedback и продолжаем цикл. fallback уже сохранён выше → пустого результата не будет,
        # а MAX_SCRIPT_ATTEMPTS/best-фолбэк ниже гарантируют завершение (не бесконечный цикл).
        shared = _loop_overlap(sc)
        if len(shared) < 2:
            hk = ", ".join(sorted(_content_words(sc.get("hook", "")))[:4]) or "the hook's key words"
            feedback = (f"\n\nClose the loop: the final outro line must echo the hook verbatim, "
                        f"repeating the hook's CHARACTERISTIC phrase (callback), NOT the topic noun "
                        f"(reuse key words: {hk}).")
            continue
        # S4(#4) — детерминированный гейт ПЕРВОГО сегмента (холодный старт 0-3с): segment-1 должен
        # сразу давать конкретику, а не читаться как сетап. Зеркалит паттерн WEAK_STARTERS (дешёвая
        # механическая проверка без LLM). Идёт в тот же feedback-цикл (continue) — fallback уже сохранён
        # выше, MAX_SCRIPT_ATTEMPTS гарантирует завершение → пустого результата не будет.
        # Логика И-ИЛИ (намеренно не строгая, чтобы не зарубать всё подряд и не жечь все попытки):
        # ПРИНИМАЕМ если есть «конкретность» (цифра / Заглавное имя собственное / сильный глагол действия)
        # ИЛИ длина ≤14 слов. ОТКЛОНЯЕМ только явный сетап: дискурс-открывашка ЛИБО (нет цифры/имени И >16 слов).
        seg1 = (sc.get("segments") or [{}])[0].get("text", "") if sc.get("segments") else ""
        if seg1:
            sw = seg1.split()
            n_words = len(sw)
            SETUP_OPENERS = ('дело в том', 'на самом деле', 'представь', 'начнём с', 'начнем с',
                             'во-первых', 'давай разберём', 'давай разберем', 'давайте разберём',
                             'для начала', 'first', 'let me explain', 'to begin', 'to start',
                             "let's start", 'lets start', 'imagine')
            low1 = seg1.lower().lstrip('—–-«"\'.,!? ')
            STRONG_VERBS = frozenset({
                'теряешь', 'теряют', 'теряете', 'потерял', 'потеряешь', 'потеряете', 'крадут', 'украл',
                'платишь', 'переплачиваешь', 'переплатил', 'выбрасываешь', 'выкидываешь', 'ломается',
                'сломал', 'разрушает', 'убивает', 'обманывают', 'обманул', 'врут', 'соврал', 'прячут',
                'скрывают', 'скрыл', 'забирают', 'забрал', 'отнимают', 'остановись', 'перестань',
                'смотри', 'проверь', 'удали', 'выключи', 'провалишь', 'провалил', 'забыл', 'забываешь',
                'lose', 'lost', 'steal', 'stole', 'stop', 'delete', 'pay', 'overpay', 'overpaid',
                'waste', 'wasted', 'break', 'broke', 'kills', 'killed', 'destroys', 'destroyed',
                'lie', 'lied', 'hide', 'hides', 'hid', 'forget', 'forgot', 'check', 'avoid', 'ruins'})
            has_number = bool(re.search(r'\d', seg1))
            # Заглавное имя собственное: слово с большой буквы НЕ в начале фразы (исключаем сам старт).
            has_proper = bool(re.search(r'(?<=\S\s)[A-ZА-ЯЁ][\w-]{2,}', seg1)) or \
                bool(re.search(r'[A-Z][a-z]+', " ".join(sw[1:])))
            first_w = re.sub(r'[^\w]', '', sw[0].lower()) if sw else ''
            has_strong_verb = any(re.sub(r'[^\w]', '', w.lower()) in STRONG_VERBS for w in sw[:4])
            is_concrete = has_number or has_proper or has_strong_verb
            looks_setup = any(low1.startswith(op) for op in SETUP_OPENERS) or \
                (not has_number and not has_proper and n_words > 16)
            # принимаем при конкретности ИЛИ короткой длине; режем только явный сетап без конкретики
            if not (is_concrete or n_words <= 14) and looks_setup:
                feedback = "\n\nSegment 1 must not be setup: open with a concrete detail/number/name."
                continue
        # антиповтор АТОМАРНО: резервируем тему (проверка дубля + INSERT в одной транзакции),
        # чтобы параллельные сборки не плодили дубли в окне между проверкой и записью.
        # Свой ранее взятый резерв (из прошлой итерации само-улучшения) сначала освобождаем —
        # иначе следующая (часто похожая) тема столкнётся с НАШЕЙ же записью (ложный дубль).
        cand_topic = sc.get("topic", "")
        if held_topic and held_topic != cand_topic:
            _release(held_topic)
            held_topic = ""
        try:
            from pipeline import topics_db
            ok_res, match = topics_db.reserve_topic(niche_id, cand_topic,
                                                    lang=niche.get("lang", "en"))
        except Exception:  # noqa: BLE001 — БД недоступна → резерв/дедуп просто выключен
            ok_res, match = True, ""
        if not ok_res and match == cand_topic:
            ok_res = True                  # совпало с НАШИМ же резервом (та же тема на ре-генерации) — это не дубль
        if not ok_res:
            feedback = (f"\n\nThe topic '{cand_topic}' is TOO SIMILAR to an already published one ('{match}'). "
                        f"Pick a COMPLETELY DIFFERENT topic/angle within this niche.")
            continue
        if not held_topic:                 # фиксируем что держим этот резерв (для возможной замены/брака)
            held_topic = cand_topic
        sc["_reserved"] = True             # факт резерва → build решит commit/release по итогу сборки
        sc = _three_hooks(sc, niche)          # 3 варианта хука → берём сильнейший
        sc["virality"] = _virality_score(sc, niche)
        score = sc["virality"]["score"]
        if score >= VIRALITY_MIN:
            return _title_variants(sc, niche)     # A/B заголовков на финальном сценарии
        if best is None or score > best["virality"]["score"]:
            best = sc
        # направленное само-улучшение: следующую попытку строим с учётом разбора судьи
        weak = sc["virality"].get("weakest", "")
        fix = sc["virality"].get("fix", "")
        feedback = (f"\n\nThe previous version scored {score}/100. Weakest axis: {weak}. "
                    f"You MUST fix that and make the video even more gripping: {fix}. "
                    f"Aim for a flawless 100/100 (powerful hook, retention loops, value, shareability, trend).")
    if best is not None:
        return _title_variants(best, niche)
    if fallback is not None:                  # ни один не взял планку, но есть рабочий — отдаём его
        fallback.setdefault("virality", _virality_score(fallback, niche))
        return _title_variants(fallback, niche)
    return _normalize({"hook": "", "segments": [], "outro": ""}, niche)


def _polish(sc: dict, niche: dict) -> dict:
    """Второй проход-редактор: вычищает язык (англ/транслит), докручивает хук. Сохраняет число сегментов."""
    draft = {"hook": sc["hook"], "segments": [s["text"] for s in sc["segments"]], "outro": sc["outro"]}
    system = (
        "You are a strict short-video script editor. Improve the draft, rewriting ONLY the text and "
        "keeping the same number of segments and their order:\n"
        "1) Plain natural English, no filler.\n"
        "2) Sharper hook (curiosity or pattern-break), no weak openers.\n"
        "3) Each segment = one idea, short, conversational 'you'. Keep the overall length "
        "(72-85 words total — a 30-35 second video; don't inflate and don't cut in half).\n"
        "4) No fabricated numbers. Keep the hook's technique and hold the payoff until the end.\n"
        "5) If the outro echoes the hook (callback), preserve that echo verbatim.\n"
        'Return STRICT JSON: {"hook": "...", "segments": ["...", "..."], "outro": "..."} with the same segment count.'
    )
    raw = _groq(system, "Draft:\n" + json.dumps(draft, ensure_ascii=False), temp=0.6)
    d = _parse(raw)
    new_segs = d.get("segments") or []
    if len(new_segs) == len(sc["segments"]):
        for i, t in enumerate(new_segs):
            txt = _clean_line(t if isinstance(t, str) else (t.get("text") if isinstance(t, dict) else ""))
            if txt:
                sc["segments"][i]["text"] = txt
    if _clean_line(d.get("hook")):
        sc["hook"] = _clean_line(d["hook"])
    if _clean_line(d.get("outro")):
        sc["outro"] = _clean_line(d["outro"])
    # title/caption обновляем под новый хук, если они слабее
    if not sc.get("title") or len(sc["title"]) < 8:
        sc["title"] = sc["hook"][:90]
    return sc


_WEAK_FIRST = frozenset({'знаешь', 'кстати', 'итак', 'короче', 'значит', 'многие', 'представь', 'слушай',
                         'ну', 'а', 'so', 'well', 'you', 'imagine', 'ever', 'okay', 'basically', 'the', 'this'})
_CURIOSITY = ('почему', 'как ', 'что ', 'секрет', 'ошибк', 'никто', 'правда', 'на самом деле', 'хватит',
              'перестан', 'why', 'how', 'what', 'secret', 'nobody', 'truth', 'mistake', 'stop', 'never')


def _viral_brief_for(niche: dict) -> dict:
    """Кэш-бриф вирал-сигналов ниши (hook_patterns/title_patterns). Fail-safe, гейт CF_VIRAL."""
    if os.environ.get("CF_VIRAL", "1") == "0":
        return {}
    try:
        from pipeline import heatmap
        vq = (niche.get("broll_hint", "") or niche.get("title", "")).split(",")[0].strip()
        return heatmap.viral_brief(vq, lang=niche.get("lang", "ru")) if vq else {}
    except Exception:  # noqa: BLE001
        return {}


def _score_hook(h: str, patterns=None) -> float:
    """Детерминированный скоринг силы хука (без LLM): длина ≤14, сильное первое слово, число-триггер,
    любопытство/петля, штраф за клише; бонус за близость к вирал-хук-паттернам ниши."""
    words = (h or "").split()
    if not words:
        return -9.0
    s, n = 0.0, len(words)
    s += 2.0 if n <= 14 else (-3.0 if n > 16 else -0.5)
    first = re.sub(r'[^\w]', '', words[0].lower())
    s += 1.5 if (first and first not in _WEAK_FIRST) else -1.5
    low = h.lower()
    if re.search(r'\d', h):
        s += 1.0
    if any(c in low for c in _CURIOSITY):
        s += 1.0
    if any(b in low for b in _BANNED) or any(c in low for c in _CLICHE):
        s -= 2.5
    if patterns:
        toks = set(re.findall(r"\w+", re.sub(r'\d+', 'N', " ".join(words[:4]).lower())))
        for p in patterns:
            if toks & set(re.findall(r"\w+", str(p).lower())):
                s += 1.2
                break
    return round(s, 2)


def _three_hooks(sc: dict, niche: dict) -> dict:
    """Сгенерировать 3 варианта хука разными приёмами и выбрать сильнейший (1 LLM-вызов).
    A/B без публикации: модель-судья оценивает все 3 по силе крючка и возвращает лучший +
    альтернативы (кладём в sc['hook_variants'] для ручной правки/будущего реального A/B)."""
    ctx = json.dumps({"topic": sc["topic"], "hook": sc["hook"],
                      "first_segment": sc["segments"][0]["text"] if sc["segments"] else ""},
                     ensure_ascii=False)
    system = (
        "You craft viral hooks for Shorts/TikTok/Reels. Given a draft hook and topic, write 3 DIFFERENT hooks "
        "(each ≤14 words, distinct technique: curiosity / pattern-break / loss-mistake / specific number), "
        "plain English, no 'hey guys/in this video', open a loop, don't reveal the payoff. Then pick the strongest "
        '(2 stacked triggers + self-relevance). Return STRICT JSON: {"variants": ["v1","v2","v3"], "best_index": 0}'
    )
    try:
        d = _parse(_groq(system, "Context:\n" + ctx, temp=0.9))
        variants = [_clean_line(v) for v in (d.get("variants") or []) if _clean_line(v)]
        if not variants:
            return sc
        bi = d.get("best_index", 0)
        bi = bi if isinstance(bi, int) and 0 <= bi < len(variants) else 0
        # выбор НЕ «мнением LLM», а детерминированным скорингом (длина/первое слово/число/любопытство/
        # вирал-паттерн), LLM-индекс — лишь лёгкий приор. Так сильнейший хук объективнее.
        pats = [p for p in (_core.sanitize_external(x) for x in (_viral_brief_for(niche).get("hook_patterns") or [])) if p]
        scores = [_score_hook(v, pats) for v in variants]
        scores[bi] += 0.8
        best_i = max(range(len(variants)), key=lambda i: scores[i])
        best = variants[best_i]
        if len(best.split()) <= 16:           # защита от случайно длинного варианта
            sc["hook"] = best
            sc["hook_variants"] = variants
            sc["hook_scores"] = scores
            if not sc.get("title") or len(sc["title"]) < 8:
                sc["title"] = best[:90]
    except Exception:  # noqa: BLE001 — улучшение необязательно, остаётся исходный хук
        pass
    return sc


def _title_variants(sc: dict, niche: dict, n: int = 5) -> dict:
    """A/B заголовков: 3-5 кликабельных вариантов YouTube-заголовка + выбор лучшего (1 LLM-вызов).
    Кладём в sc['title_variants'] (для approval-гейта/будущего псевдо-A/B по APV). Не критично."""
    ctx = json.dumps({"topic": sc.get("topic", ""), "hook": sc.get("hook", ""),
                      "outro": sc.get("outro", "")}, ensure_ascii=False)
    system = (
        "You write clickable YouTube Shorts titles. From the topic and hook, give 5 DIFFERENT titles "
        "(each ≤60 chars: number / curiosity / pattern-break / benefit; no clickbait lies, no hashtags). "
        "Every factual claim in a title must be TRUE — never invent bans/laws/'illegal' angles. "
        'Then pick the strongest. Return STRICT JSON: {"variants": ["t1",...,"t5"], "best_index": 0}')
    # вирал-обучение: проверенные ФОРМЫ заголовков топ-роликов ниши → в промпт как структура-вдохновение
    tp = [p for p in (_core.sanitize_external(x) for x in (_viral_brief_for(niche).get("title_patterns") or [])[:5]) if p]
    if tp:
        system += "\nProven title STRUCTURES in this niche (mirror the STRUCTURE, don't copy): " + " | ".join(tp)
    try:
        d = _parse(_groq(system, "Context:\n" + ctx, temp=0.9))
        vs = [_clean_line(v) for v in (d.get("variants") or []) if _clean_line(v)][:n]
        if vs:
            bi = d.get("best_index", 0)
            bi = bi if isinstance(bi, int) and 0 <= bi < len(vs) else 0
            sc["title_variants"] = vs
            sc["title"] = (vs[bi] or sc.get("title", ""))[:90]
    except Exception:  # noqa: BLE001 — A/B заголовков необязательно
        pass
    return sc


_VIRALITY_AXES = ("hook", "retention", "value", "shareability", "trend", "loop")


def _virality_score(sc: dict, niche: dict) -> dict:
    """LLM-самооценка сценария 0-120 по 6 осям (hook/retention/value/shareability/trend/loop).
    Низкий балл → сигнал на перегенерацию. 1 LLM-вызов. Возвращает {score, breakdown, weakest, fix}."""
    # S8 — префиксуем строки ролью/позицией, чтобы судья видел, ГДЕ стоит выплата (роль уже в чанках через to_chunks).
    chunks = to_chunks(sc)
    body_total = sum(1 for c in chunks if c.get("role") == "body")
    lines, seg_i = [], 0
    for c in chunks:
        role = c.get("role")
        if role == "hook":
            tag = "HOOK:"
        elif role == "outro":
            tag = "OUTRO:"
        else:
            seg_i += 1
            tag = f"SEG {seg_i}/{body_total}:"
        lines.append(f"{tag} {c.get('text', '')}")
    spoken = "\n".join(lines)
    system = (
        "You are a strict viral-Shorts editor. Score this script objectively and harshly (most score 40-65). "
        "6 axes 0-20 each: hook, retention, value, shareability, trend, "
        "loop (outro echoes the hook verbatim → seamless replay, makes you rewatch from the start? "
        "the echo repeats the hook's CHARACTERISTIC phrase (callback), not the topic noun). "
        "Lines are tagged with role and position (HOOK / SEG i/N / OUTRO). "
        "PENALIZE retention if the main payoff/reveal lands earlier than the last ~20% of segments "
        "(viewer got everything early and left). "
        "PENALIZE retention if the first segment is a slow setup/backstory with no specifics. "
        'Return STRICT JSON: {"hook":N,"retention":N,"value":N,"shareability":N,"trend":N,"loop":N,'
        '"weakest":"axis","fix":"one concrete one-line improvement"}'
    )
    try:
        d = _parse(_groq(system, "Script:\n" + spoken, temp=0.3))
        axes = {k: float(d.get(k, 0) or 0) for k in _VIRALITY_AXES}
        total = round(sum(min(20.0, max(0.0, v)) for v in axes.values()))
        return {"score": total, "breakdown": {k: round(v) for k, v in axes.items()},
                "weakest": _clean_line(d.get("weakest")), "fix": _clean_line(d.get("fix"))}
    except Exception:  # noqa: BLE001 — если судья сбоит, не блокируем (нейтральный балл)
        return {"score": 84, "breakdown": {}, "weakest": "", "fix": "", "_skipped": True}


VIRALITY_MIN = 90          # планка из 120 (6 осей×20); ~75/100 в старой 5-осевой шкале — та же селективность
MAX_SCRIPT_ATTEMPTS = 8    # попыток с само-улучшением, потом берём лучший


def _parse(raw: str) -> dict:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            return json.loads(m.group(0))
        raise RuntimeError(f"Groq вернул не-JSON: {raw[:200]}")


def _normalize(data: dict, niche: dict) -> dict:
    hook = _clean_line(data.get("hook"))
    outro = _clean_line(data.get("outro"))
    segs = []
    for s in (data.get("segments") or []):
        if isinstance(s, str):
            txt, q = _clean_line(s), ""
        else:
            txt, q = _clean_line(s.get("text")), _clean_line(s.get("broll_query"))
        if txt:
            segs.append({"text": txt, "broll_query": q or niche.get("broll_hint", "abstract").split(",")[0].strip()})
    segs = segs[:6]

    base_q = niche.get("broll_hint", "abstract background").split(",")[0].strip()
    hashtags = []
    for h in (data.get("hashtags") or []):
        s = _clean_line(h)  # безопасно стрингифицирует int/None/dict
        if s:
            hashtags.append(s if s.startswith("#") else f"#{s}")
    if not hashtags:
        hashtags = niche.get("hashtags", [])

    return {
        "topic": _clean_line(data.get("topic")) or (segs[0]["text"][:40] if segs else "video"),
        "lang": niche.get("lang", "en"),
        "voice": niche.get("voice", "en-US-GuyNeural"),
        "rate": niche.get("rate", "+0%"),
        "speed": float(niche.get("speed", 1.2)),  # темп речи (atempo); 1.2 = бодрее для shorts
        "hook": hook,
        "segments": segs,
        "outro": outro,
        "title": (_clean_line(data.get("title")) or hook)[:90],
        "thumb_text": _clean_line(data.get("thumb_text")),   # короткая законченная фраза для обложки
        "description": _clean_line(data.get("description")),
        "caption": _clean_line(data.get("caption")) or hook,
        "hashtags": hashtags,
        "character": _clean_line(data.get("character")),   # для story: единый персонаж во всех кадрах
        "_hook_query": (segs[0]["broll_query"] if segs else base_q),
    }


_BANNED = ("в этом видео", "сегодня поговорим", "привет, ребят", "а вы когда-нибудь задумыв",
           "о чём никто не говорит", "hey guys", "in this video", "welcome back")

# Клише/вода — детерминированный свип по ВСЕМУ тексту (не только хук/outro). Подаётся в feedback-цикл,
# чтобы LLM заменил их конкретикой. Дешёвый (~1мс), без LLM. Ловит то, что ANTI_SLOP лишь «просит».
_CLICHE = ("в современном мире", "не секрет", "играет важную роль", "на сегодняшний день",
           "давайте разберёмся", "давай разберёмся", "как известно", "как оказалось",
           "ни для кого не секрет", "стоит отметить", "следует отметить", "трудно переоценить",
           "в наше время", "испокон веков", "не за горами", "так уж сложилось",
           "in today's world", "it's no secret", "needless to say", "at the end of the day",
           "when it comes to", "little did")

# Стоп-слова (ru+en) для проверки петли хук↔outro: служебные слова не считаются «значимыми».
_LOOP_STOP = frozenset((
    "и", "а", "но", "да", "или", "не", "ни", "же", "бы", "ли", "то", "вот", "уж",
    "в", "во", "на", "за", "по", "под", "над", "из", "от", "до", "у", "о", "об", "с", "со",
    "к", "ко", "для", "без", "при", "про", "через", "это", "этот", "эта", "эти", "тот", "та", "те",
    "как", "что", "чтобы", "когда", "где", "куда", "там", "тут", "здесь", "уже", "ещё", "еще",
    "я", "ты", "он", "она", "оно", "мы", "вы", "они", "мой", "твой", "наш", "ваш", "свой",
    "его", "её", "ее", "их", "себя", "сам", "так", "тоже", "всё", "все", "весь", "вся",
    "был", "была", "было", "были", "есть", "быть", "будет", "будут",
    "the", "a", "an", "and", "or", "but", "not", "no", "of", "to", "in", "on", "at", "by",
    "for", "with", "from", "as", "is", "are", "was", "were", "be", "been", "this", "that",
    "these", "those", "it", "its", "you", "your", "we", "our", "they", "their", "i", "my",
    "he", "she", "his", "her", "do", "does", "did", "can", "will", "just", "so", "if", "then",
))


def _content_words(text: str) -> set[str]:
    """Значимые слова (content words): нижний регистр, без пунктуации, минус стоп-слова и чистые числа."""
    out = set()
    for w in re.findall(r"[\w']+", str(text or "").lower(), re.UNICODE):
        if len(w) < 3 or w in _LOOP_STOP or w.isdigit():
            continue
        out.add(w)
    return out


def _loop_overlap(sc: dict) -> set[str]:
    """Общие значимые слова хука и outro — признак замкнутой петли (бесшовный replay).
    S4(#21) — вычитаем слова темы-носителя: тривиальный повтор тематического существительного
    (например, само название ниши) НЕ должен засчитываться за петлю; нужна перекличка
    ХАРАКТЕРНОЙ фразы хука (callback), а не повтор предмета разговора."""
    return ((_content_words(sc.get("hook", "")) & _content_words(sc.get("outro", "")))
            - _content_words(sc.get("topic", "")))


def validate(sc: dict) -> tuple[bool, str]:
    """Гейт качества сценария (этап 4): длина/хук/сегменты/outro/бан-фразы. По ресёрчу 2026."""
    spoken = (sc.get("hook", "") + " " + " ".join(s.get("text", "") for s in sc.get("segments", []))
              + " " + sc.get("outro", "")).split()
    n = len(spoken)
    if not (62 <= n <= 92):
        return False, f"word count {n} (need ~72-85 → 30-35s video)"
    if len(sc.get("hook", "").split()) > 16:
        return False, "hook longer than 14-16 words"
    # S4(#4) — детерминированный гейт слабого ПЕРВОГО слова хука (служебное → вялый старт, скип).
    # Идёт в существующий feedback-цикл (validate()==False → continue), пустого результата не даёт.
    WEAK_STARTERS = frozenset({'знаешь', 'кстати', 'итак', 'короче', 'значит', 'многие', 'представь',
                               'слушай', 'so', 'well', 'you', 'imagine', 'ever', 'okay', 'basically'})
    first = re.sub(r'[^\w]', '', sc.get('hook', '').split()[0].lower()) if sc.get('hook', '').split() else ''
    if first in WEAK_STARTERS:
        return False, "weak first word in the hook"
    if len(sc.get("segments", [])) < 4:
        return False, f"segments {len(sc.get('segments', []))} (<4)"
    if not sc.get("outro"):
        return False, "no outro"
    low = (sc.get("hook", "") + " " + sc.get("outro", "")).lower()
    if any(b in low for b in _BANNED):
        return False, "banned phrase in hook/outro"
    # клише/вода по ВСЕМУ тексту (хук+сегменты+outro) → конкретный feedback с найденной фразой
    full = " ".join(spoken).lower()
    hit = next((c for c in _CLICHE if c in full), None)
    if hit:
        return False, f"cliché/filler in text: '{hit}' — replace it with specifics (number / action / name / fact)"
    return True, "ok"


_BANRISK_CATS = ("medical promises/diagnoses/cures; politics/hate speech; 18+/sexual content; "
                 "violence/gore/shock; drugs; weapons; gambling framed as guaranteed income; "
                 "financial GUARANTEES of income / get-rich schemes; self-harm; life-threatening advice; "
                 "blatant copyright (recognizable brands/characters/tracks); misinformation")


def ban_risk(sc: dict, niche: dict | None = None) -> dict:
    """LLM-модерация сценария на риск блокировки/демонетизации/бана аккаунта площадкой.
    Возвращает {"risk":"low|medium|high","categories":[...],"reason":"..."}. Гейт CF_BANRISK (по умолч. вкл).
    Fail-OPEN: при недоступности LLM → risk=low (сбой модерации НЕ должен рубить нормальный контент).
    Один бан убивает канал — дешёвый текстовый проход окупается страховкой аккаунтов для 24/7."""
    if os.environ.get("CF_BANRISK", "1").strip().lower() in ("0", "false", "no", "off"):
        return {"risk": "low", "categories": [], "reason": "gate disabled"}
    text = " ".join([sc.get("hook", ""), sc.get("topic", "")]
                    + [s.get("text", "") for s in sc.get("segments", [])]
                    + [sc.get("outro", "")]).strip()[:2500]
    if not text:
        return {"risk": "low", "categories": [], "reason": "empty text"}
    system = (
        "You are a strict but reasonable short-video moderator (YouTube Shorts / TikTok / Reels). "
        "Assess the RISK of this TEXT getting the video blocked, demonetized, or the account banned. Risk categories: "
        + _BANRISK_CATS + ". Educational facts, history, science, psychology, lifehacks, business stories are "
        "NORMAL (low), even on serious topics. medium — borderline (a mention without advocacy). "
        "high — ONLY a clear platform-rules violation (advocacy, harm instructions, income guarantees, 18+). "
        'Return STRICT JSON: {"risk":"low|medium|high","categories":["..."],"reason":"brief why"}.')
    try:
        d = _parse(_groq(system, "Video text:\n" + text, temp=0.2, max_tokens=200, json_mode=True))
        risk = str(d.get("risk", "low")).lower().strip()
        if risk not in ("low", "medium", "high"):
            risk = "low"
        cats = [str(c) for c in (d.get("categories") or [])][:6]
        return {"risk": risk, "categories": cats, "reason": str(d.get("reason", ""))[:200]}
    except Exception as e:  # noqa: BLE001 — модерация упала → fail-open (не блокируем)
        _core.log_error("script.ban_risk", e)
        return {"risk": "low", "categories": [], "reason": "LLM unavailable (fail-open)"}


def to_chunks(script: dict) -> list[dict]:
    """Развернуть сценарий в линейный список озвучиваемых кусков с ролями и broll-запросами."""
    chunks = []
    if script.get("hook"):
        chunks.append({"text": script["hook"], "broll_query": script.get("_hook_query", ""), "role": "hook"})
    for s in script.get("segments", []):
        chunks.append({"text": s["text"], "broll_query": s.get("broll_query", ""), "role": "body"})
    if script.get("outro"):
        last_q = script["segments"][-1]["broll_query"] if script.get("segments") else script.get("_hook_query", "")
        chunks.append({"text": script["outro"], "broll_query": last_q, "role": "outro"})
    return chunks


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
    import core
    core.load_local_secrets()
    nid = sys.argv[1] if len(sys.argv) > 1 else "ai_lifehacks"
    topic = sys.argv[2] if len(sys.argv) > 2 else None
    niche = core.get_niche(nid)
    sc = generate(niche, topic=topic, avoid=core.recent_topics(nid))
    print(json.dumps(sc, ensure_ascii=False, indent=2))
