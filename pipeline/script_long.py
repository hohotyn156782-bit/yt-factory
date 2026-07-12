"""Генерация сценария ДЛИННОГО документального видео (10-12 мин, 16:9) через LLM-каскад.

Двухэтапная схема: (1) outline — заголовок/главы/биты одним вызовом, (2) последовательное
разворачивание глав с передачей «хвоста» предыдущей главы (нарративная связность), затем
outro, описание и polish-проход. Анти-шаблонная вариативность (YouTube 2026 «inauthentic
content»): детерминированный сид из hash темы → число глав (5-7), формула хука (4 именованных),
позиция pattern-interrupt recap (глава 3 или 4), ритм предложений. Ручки уходят В ПРОМПТЫ,
поэтому соседние выпуски различаются структурно, а не только словами.

Гейты: механический validate_long() (объём/главы/хук/описание/бан-фразы/broll) с фидбек-циклом
до 3 попыток + LLM-судья _quality_score() (6 осей, планка ~70/100, берём лучший из попыток).

Публичный API: generate_long(niche, topic, avoid=None) -> dict; to_chunks(script) -> list[dict]
(та же структура чанков, что у script.to_chunks() — voice.synthesize ест без изменений).
"""
import hashlib
import json
import random
import re

import sys as _sys
import pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent.parent))
import core as _core  # noqa: E402

from pipeline import llm  # noqa: E402
# переиспользуем хелперы и модерацию шортс-генератора (единый парс/чистка/ban-гейт на оба формата)
from pipeline.script import _parse, _clean_line, ban_risk  # noqa: E402,F401

MAX_LONG_ATTEMPTS = 3      # полных перегенераций с фидбеком, потом берём лучший по судье
QUALITY_MIN = 70           # планка LLM-судьи из 100

# ── Анти-шаблонная вариативность ──────────────────────────────────────────────
# 4 именованные формулы хука; выбор детерминирован сидом темы → одна тема всегда даёт
# один и тот же скелет (воспроизводимость), а разные темы — разные структуры.
HOOK_FORMULAS = {
    "cold_open": (
        "COLD-OPEN SCENE: drop the viewer into ONE vivid, concrete moment — a specific date, "
        "a place, a person mid-action — as if the camera is already rolling. No throat-clearing."),
    "shocking_number": (
        "SHOCKING NUMBER: open with one staggering, well-documented figure and immediately make it "
        "tangible with a modern comparison the viewer can feel."),
    "question_stack": (
        "QUESTION STACK: open with 2-3 escalating questions the viewer realizes they cannot answer — "
        "each question raises the stakes of the last."),
    "everyone_believes": (
        "'EVERYONE BELIEVES X, BUT': state the common belief in one sentence, then flip it with the "
        "documented reality that contradicts it."),
}

_RHYTHM = {
    "short-punchy": (
        "Sentence rhythm: SHORT and PUNCHY. Mostly 5-12 word sentences. Occasional one-word or "
        "two-word sentence for impact. Like that."),
    "flowing": (
        "Sentence rhythm: FLOWING. Varied-length sentences that build momentum, longer descriptive "
        "lines broken by a short hard-hitting one at each key beat."),
}

# бан-фразы (мгновенный признак шаблонного AI-видео); проверяются механически в validate_long
BANNED_FILLERS = (
    "in this video", "in today's video", "without further ado", "let's dive in",
    "let's get started", "hey guys", "welcome back", "before we begin",
    "stick around", "buckle up", "it's no secret", "at the end of the day",
)

FACTUAL = (
    "FACTUAL ACCURACY (non-negotiable): use ONLY well-documented history. NEVER invent quotes, "
    "dialogue, or statistics. Prefer widely attested facts (the kind found in major encyclopedias "
    "and standard histories). If a detail is disputed or uncertain, omit it or attribute it "
    "('historians estimate...'). Concrete numbers, dates, and names in every chapter — but only real ones."
)

RETENTION = (
    "RETENTION MECHANICS: every chapter ENDS with a one-sentence forward tease (open loop) that makes "
    "the next chapter's question irresistible. Each chapter answers the previous tease and plants a new "
    "question. No filler phrases (" + ", ".join(f"'{b}'" for b in BANNED_FILLERS[:6]) + "...). "
    "Dense with concrete numbers, dates, and names. Write for the ear: it will be read aloud by a narrator."
)


def _variance(topic: str) -> dict:
    """Детерминированные ручки вариативности из hash темы — уходят в промпты, чтобы соседние
    видео различались структурно (число глав / формула хука / позиция recap / ритм)."""
    seed = int(hashlib.sha256((topic or "").strip().lower().encode()).hexdigest()[:16], 16)
    rng = random.Random(seed)
    return {
        "n_chapters": rng.choice([5, 6, 7]),
        "hook_formula": rng.choice(sorted(HOOK_FORMULAS)),
        "recap_chapter": rng.choice([3, 4]),          # 1-based глава, открывающаяся recap-перебивкой
        "rhythm": rng.choice(sorted(_RHYTHM)),
    }


# ── Этап 1: outline ───────────────────────────────────────────────────────────

def _outline(niche: dict, topic: str, var: dict, avoid: list[str] | None, feedback: str) -> dict:
    n = var["n_chapters"]
    system = (
        "You are a story architect for long-form YouTube history documentaries (10-12 minutes) "
        "on a US-audience channel.\n"
        f"NICHE: {niche.get('topic_brief', 'history documentaries')}\n\n"
        f"{FACTUAL}\n\n"
        "Design the skeleton of ONE documentary:\n"
        f"  • EXACTLY {n} chapters. Each chapter heading is CURIOSITY-STYLED (it becomes a public "
        "YouTube chapter title): a specific tension or question, never a dry label like 'Background' "
        "or 'Conclusion'.\n"
        "  • Each chapter gets 3-5 'beats' — concrete documented facts/events/turns it will cover "
        "(with real names, dates, numbers).\n"
        "  • The chapters form ONE narrative arc: rise → complications → turning point → fall/legacy, "
        "each chapter ending on an open question the next one answers.\n"
        "  • title: under 60 characters, curiosity formula (a gap the viewer must close), no clickbait lies.\n"
        "  • thumb_text: 2-4 punchy words for the thumbnail (a complete phrase).\n"
        "  • tags: 5-10 YouTube search tags (lowercase, no '#').\n"
        "  • further_reading: 2-4 REAL, well-known books or encyclopedia entries on this topic "
        "(exact real titles and authors only — these are printed in the video description; if unsure "
        "of a book, use a major encyclopedia entry instead).\n"
        "  • core_promise: one sentence — the single question/promise the whole video pays off.\n"
        "  • related_tease: a neighboring topic to tease in the outro.\n\n"
        "Return STRICT JSON:\n"
        '{"title": "...", "thumb_text": "...", "tags": ["..."], "core_promise": "...",\n'
        ' "chapters": [{"heading": "...", "beats": ["...", "..."]}],\n'
        ' "further_reading": ["Title — Author", "..."], "related_tease": "..."}'
    )
    avoid_line = ""
    if avoid:
        snip = " | ".join(_clean_line(t)[:70] for t in avoid[:12] if t)
        if snip:
            avoid_line = "\nDO NOT overlap with these recent videos: " + snip
    user = (f"Documentary topic: {topic}\n"
            f"Chapters: exactly {n}.{avoid_line}{feedback}")
    return _parse(llm.chat(system, user, json_mode=True, max_tokens=4000, temp=0.9))


# ── Этап 2: хук, главы (последовательно, с хвостом предыдущей), outro ─────────

def _gen_hook(topic: str, outline: dict, var: dict) -> str:
    system = (
        "You write opening hooks for long-form YouTube history documentaries. "
        "Write the spoken opening narration (~70-90 words).\n"
        f"HOOK FORMULA to use: {HOOK_FORMULAS[var['hook_formula']]}\n"
        f"{_RHYTHM[var['rhythm']]}\n\n"
        "HARD RULES:\n"
        "  • The FIRST sentence (max 20 words) must state the video's core promise — what the viewer "
        "will understand by the end.\n"
        "  • Concrete: real names, dates, numbers. " + FACTUAL.split(':', 1)[1].strip() + "\n"
        "  • End the hook on an open loop leading into chapter 1.\n"
        "  • No filler: never " + ", ".join(f"'{b}'" for b in BANNED_FILLERS[:4]) + ".\n"
        'Return STRICT JSON: {"hook": "..."}'
    )
    user = (f"Topic: {topic}\nCore promise: {outline.get('core_promise', '')}\n"
            f"Chapter 1 heading: {(outline.get('chapters') or [{}])[0].get('heading', '')}")
    return _clean_line(_parse(llm.chat(system, user, json_mode=True, max_tokens=4000, temp=0.85)).get("hook"))


def _gen_chapter(topic: str, outline: dict, idx: int, prev_tail: str, var: dict,
                 w_lo: int, w_hi: int) -> dict:
    """Развернуть главу idx (0-based). prev_tail — хвост предыдущей главы для бесшовного нарратива."""
    chapters = outline.get("chapters") or []
    ch = chapters[idx]
    n = len(chapters)
    is_last = idx == n - 1
    next_heading = chapters[idx + 1].get("heading", "") if not is_last else ""
    recap_line = ""
    if idx + 1 == var["recap_chapter"]:
        recap_line = ("\n  • PATTERN-INTERRUPT: open THIS chapter with a 1-2 sentence mid-video recap — "
                      "re-ground the viewer in what is at stake so far and re-hook them for the second "
                      "half. Then continue the narrative.")
    tease_line = ("\n  • This is the FINAL chapter: land the biggest payoff of the core promise, then end "
                  "with ONE sentence that opens a bigger reflective question (it leads into the outro)."
                  if is_last else
                  f"\n  • END with a one-sentence forward tease (open loop) pulling into the next chapter "
                  f"('{next_heading}') — without naming it as 'the next chapter'.")
    system = (
        "You write narration chapters for a long-form YouTube history documentary. "
        # LLM стабильно недобирает объём ~20% → просим верх окна и жёстко предупреждаем про низ
        f"Write chapter {idx + 1} of {n} — spoken narration only. HARD MINIMUM {w_lo} words, "
        f"aim for {(w_lo + w_hi) // 2 + 30}-{w_hi} words. Chapters under {w_lo} words are "
        "automatically rejected — be generous with concrete detail, not with filler.\n\n"
        f"{FACTUAL}\n\n{RETENTION}\n\n{_RHYTHM[var['rhythm']]}\n\n"
        "RULES for this chapter:\n"
        "  • Cover the beats below with concrete documented facts (names, dates, numbers)."
        + recap_line + tease_line + "\n"
        "  • Do not re-explain what previous chapters already covered (except an explicitly requested recap).\n"
        "  • broll_queries: 2-4 ENGLISH stock-footage search phrases (4-7 words each) — CONCRETE visual "
        "scenes matching this chapter's content that stock libraries can plausibly have: era-evocative "
        "objects, landscapes, sea/city/workshop scenes, close-ups of maps, coins, documents, tools. "
        "FORBIDDEN: abstract, 3d render, patterns, particles, motion graphics, logo, neon shapes.\n"
        'Return STRICT JSON: {"text": "...", "broll_queries": ["...", "..."]}'
    )
    beats = "\n".join(f"  - {b}" for b in (ch.get("beats") or []))
    prev_line = (f"\nPREVIOUS CHAPTER ENDED WITH: \"{prev_tail}\" — continue seamlessly from there, "
                 f"do not repeat it." if prev_tail else "")
    user = (f"Topic: {topic}\nChapter {idx + 1} heading: {ch.get('heading', '')}\n"
            f"Beats to cover:\n{beats}{prev_line}")
    d = _parse(llm.chat(system, user, json_mode=True, max_tokens=4000, temp=0.8))
    qs = [_clean_line(q) for q in (d.get("broll_queries") or []) if _clean_line(q)][:4]
    return {"heading": _clean_line(ch.get("heading")), "text": _clean_line(d.get("text")),
            "broll_queries": qs}


def _expand_chapter(ch: dict, topic: str, w_lo: int, w_hi: int) -> dict:
    """Точечный до-раскрут короткой главы (дешевле полной перегенерации драфта).
    Fail-safe: при сбое/невалидном ответе возвращаем исходную главу."""
    system = (
        "You expand a documentary narration chapter that came out too short. "
        f"Rewrite it to {w_lo + 20}-{w_hi} words by ADDING concrete documented detail "
        "(names, dates, numbers, vivid specifics) — no filler, no repetition. "
        "Keep the final forward-tease sentence exactly at the end. "
        'Return STRICT JSON: {"text": "..."}'
    )
    try:
        d = _parse(llm.chat(system, f"Topic: {topic}\nChapter heading: {ch.get('heading', '')}\n"
                            f"Current text:\n{ch.get('text', '')}",
                            json_mode=True, max_tokens=4000, temp=0.7))
        txt = _clean_line(d.get("text"))
        if txt and len(txt.split()) >= w_lo:
            return {**ch, "text": txt}
    except Exception:  # noqa: BLE001
        pass
    return ch


def _gen_outro(topic: str, outline: dict, last_tail: str, niche: dict) -> str:
    system = (
        "You write outros for long-form YouTube history documentaries: ~40-60 spoken words. "
        "Structure: one sentence closing the story's arc → tease a RELATED topic the channel covers "
        "(open loop for the next video) → a short natural subscribe CTA"
        + (f" (in the spirit of '{niche.get('cta')}')" if niche.get("cta") else "") +
        ". No 'like and subscribe' begging, no filler.\n"
        'Return STRICT JSON: {"outro": "..."}'
    )
    user = (f"Topic: {topic}\nRelated topic to tease: {outline.get('related_tease', '')}\n"
            f"The final chapter ended with: \"{last_tail}\"")
    return _clean_line(_parse(llm.chat(system, user, json_mode=True, max_tokens=4000, temp=0.8)).get("outro"))


def _gen_description(topic: str, title: str, hook: str, headings: list[str], longer: bool = False) -> dict:
    system = (
        "You write YouTube descriptions for long-form history documentaries.\n"
        "  • first_line: one keyword-rich sentence (search terms a viewer would type; no hashtags).\n"
        f"  • summary: {'170-220' if longer else '130-190'} words — a substantive, spoiler-light summary "
        "of what the documentary covers and why it matters. Plain English, no hype clichés.\n"
        'Return STRICT JSON: {"first_line": "...", "summary": "..."}'
    )
    user = f"Title: {title}\nTopic: {topic}\nOpening narration: {hook}\nChapters:\n" + \
        "\n".join(f"  {i + 1}. {h}" for i, h in enumerate(headings))
    return _parse(llm.chat(system, user, json_mode=True, max_tokens=4000, temp=0.7))


def _assemble_description(d: dict, sources: list[str]) -> str:
    lines = [_clean_line(d.get("first_line")), "", _clean_line(d.get("summary")), ""]
    if sources:
        lines.append("Further reading:")
        lines += [f"• {s}" for s in sources]
        lines.append("")
    lines += ["Chapters:", "{CHAPTERS}"]   # плейсхолдер: build подставит реальные таймкоды глав
    return "\n".join(lines)


# ── Этап 3: polish ────────────────────────────────────────────────────────────

def _polish_long(sc: dict, var: dict) -> dict:
    """Финальный редакторский проход по всей озвучке. Fail-safe: главу принимаем только если
    её объём остался в валидном окне; при любом сбое возвращаем исходник."""
    draft = {"hook": sc["hook"], "chapters": [c["text"] for c in sc["chapters"]], "outro": sc["outro"]}
    system = (
        "You are a strict documentary script editor. Polish this narration draft, rewriting ONLY the text:\n"
        "  • Remove filler, repetition, and throat-clearing; keep EVERY fact, name, date, and number.\n"
        "  • Keep each chapter's length within ±10% of the draft.\n"
        "  • Keep every chapter's final forward-tease sentence (open loop).\n"
        "  • Keep the hook's first sentence stating the core promise.\n"
        f"  • {_RHYTHM[var['rhythm']]}\n"
        "  • Written for the ear — a narrator reads it aloud.\n"
        'Return STRICT JSON: {"hook": "...", "chapters": ["...", "..."], "outro": "..."} '
        "with the SAME chapter count."
    )
    try:
        d = _parse(llm.chat(system, "Draft:\n" + json.dumps(draft, ensure_ascii=False),
                            json_mode=True, max_tokens=6000, temp=0.5))
    except Exception:  # noqa: BLE001 — polish необязателен
        return sc
    new_ch = d.get("chapters") or []
    if len(new_ch) == len(sc["chapters"]):
        for i, t in enumerate(new_ch):
            txt = _clean_line(t)
            if txt and 150 <= len(txt.split()) <= 350:
                sc["chapters"][i]["text"] = txt
    hk = _clean_line(d.get("hook"))
    if hk and 55 <= len(hk.split()) <= 105:
        sc["hook"] = hk
    ot = _clean_line(d.get("outro"))
    if ot and 25 <= len(ot.split()) <= 75:
        sc["outro"] = ot
    return sc


# ── Гейты ─────────────────────────────────────────────────────────────────────

_SENT_SPLIT = re.compile(r"(?<=[.!?…])\s+")


def _first_sentence(text: str) -> str:
    parts = _SENT_SPLIT.split((text or "").strip(), maxsplit=1)
    return parts[0] if parts else ""


def _spoken_words(sc: dict) -> int:
    return len((sc.get("hook", "") + " "
                + " ".join(c.get("text", "") for c in sc.get("chapters", []))
                + " " + sc.get("outro", "")).split())


def validate_long(sc: dict) -> tuple[bool, str]:
    """Механический гейт длинного сценария: объём/главы/хук/описание/бан-фразы/broll."""
    total = _spoken_words(sc)
    if not (1350 <= total <= 2100):
        return False, f"total spoken words {total} (need 1350-2100 for a 10-12 min video)"
    chapters = sc.get("chapters") or []
    if not (5 <= len(chapters) <= 7):
        return False, f"chapter count {len(chapters)} (need 5-7)"
    for i, ch in enumerate(chapters):
        w = len(ch.get("text", "").split())
        if not (150 <= w <= 350):
            return False, f"chapter {i + 1} has {w} words (need 150-350)"
        if not ch.get("heading"):
            return False, f"chapter {i + 1} has no heading"
        qs = [q for q in (ch.get("broll_queries") or []) if _clean_line(q)]
        if len(qs) < 2:
            return False, f"chapter {i + 1} has {len(qs)} broll_queries (need 2-4)"
        if any(not q.isascii() for q in qs):
            return False, f"chapter {i + 1} has a non-English broll_query"
    fs = _first_sentence(sc.get("hook", ""))
    if not fs:
        return False, "empty hook"
    if len(fs.split()) > 22:
        return False, f"hook's first sentence is {len(fs.split())} words (max 22; it must state the core promise)"
    if len(sc.get("description", "").split()) < 150:
        return False, f"description is {len(sc.get('description', '').split())} words (need 150+)"
    if not sc.get("outro"):
        return False, "no outro"
    full = (sc.get("hook", "") + " " + " ".join(c.get("text", "") for c in chapters)
            + " " + sc.get("outro", "")).lower()
    hit = next((b for b in BANNED_FILLERS if b in full), None)
    if hit:
        return False, f"banned filler phrase in narration: '{hit}'"
    return True, "ok"


_QUALITY_AXES = ("hook", "curiosity", "specificity", "open_loops", "safety", "originality")


def _quality_score(sc: dict) -> dict:
    """LLM-судья длинного сценария: 6 осей × 0-20 → нормируем к 100. Fail-safe (нейтральный балл)."""
    lines = [f"TITLE: {sc.get('title', '')}", f"HOOK: {sc.get('hook', '')}"]
    for i, ch in enumerate(sc.get("chapters", [])):
        lines.append(f"CHAPTER {i + 1} — {ch.get('heading', '')}:\n{ch.get('text', '')}")
    lines.append(f"OUTRO: {sc.get('outro', '')}")
    system = (
        "You are a harsh editor of long-form YouTube history documentaries. Score this script "
        "objectively and strictly (most scripts earn 8-14 per axis). 6 axes, 0-20 each:\n"
        "  • hook — does the opening promise + first 30 seconds force you to keep watching?\n"
        "  • curiosity — do chapter headings and chapter endings form a chain of must-answer questions?\n"
        "  • specificity — density of concrete dates, numbers, and names (penalize vague generalities)?\n"
        "  • open_loops — does every chapter end on a forward tease; are payoffs spaced to the end?\n"
        "  • safety — advertiser-friendly (no gore/hate/adult framing), suitable for monetization?\n"
        "  • originality — a fresh angle on the topic vs a generic encyclopedic retelling?\n"
        'Return STRICT JSON: {"hook":N,"curiosity":N,"specificity":N,"open_loops":N,"safety":N,'
        '"originality":N,"weakest":"axis","fix":"one concrete one-line improvement"}'
    )
    try:
        d = _parse(llm.chat(system, "Script:\n" + "\n\n".join(lines), json_mode=True,
                            max_tokens=1000, temp=0.3))
        axes = {k: float(d.get(k, 0) or 0) for k in _QUALITY_AXES}
        total = round(sum(min(20.0, max(0.0, v)) for v in axes.values()) / 1.2)   # 0-120 → 0-100
        return {"score": total, "breakdown": {k: round(v) for k, v in axes.items()},
                "weakest": _clean_line(d.get("weakest")), "fix": _clean_line(d.get("fix"))}
    except Exception:  # noqa: BLE001 — судья сбоит → нейтральный балл, не блокируем
        return {"score": QUALITY_MIN, "breakdown": {}, "weakest": "", "fix": "", "_skipped": True}


# ── Сборка одного драфта и публичный API ──────────────────────────────────────

def _normalize_long(niche: dict, topic: str, outline: dict, hook: str,
                    chapters: list[dict], outro: str, desc: str) -> dict:
    title = _clean_line(outline.get("title")) or topic
    if len(title) > 60:                      # <60 символов: режем по границе слова
        title = title[:60].rsplit(" ", 1)[0].rstrip(",.:;— ")
    thumb = " ".join(_clean_line(outline.get("thumb_text")).upper().split()[:4])
    tags = [_clean_line(t).lstrip("#").lower() for t in (outline.get("tags") or []) if _clean_line(t)][:10]
    if len(tags) < 5:                        # добор из хэштегов ниши до минимума
        extra = [h.lstrip("#").lower() for h in niche.get("hashtags", [])]
        tags += [t for t in extra if t and t not in tags][:5 - len(tags)]
    sources = [_clean_line(s) for s in (outline.get("further_reading") or []) if _clean_line(s)][:4]
    sc = {
        "topic": topic,
        "lang": niche.get("lang", "en"),
        "voice": niche.get("voice", "en-US-GuyNeural"),
        "rate": niche.get("rate", "+0%"),
        "speed": float(niche.get("speed", 1.0)),   # документалка — обычный темп, не шортс-1.2
        "title": title,
        "thumb_text": thumb,
        "tags": tags,
        "hook": hook,
        "chapters": chapters,
        "outro": outro,
        "description": desc,
        "sources": sources,
    }
    sc["total_words"] = _spoken_words(sc)
    return sc


def _build_draft(niche: dict, topic: str, avoid: list[str] | None, feedback: str) -> dict:
    var = _variance(topic)
    outline = _outline(niche, topic, var, avoid, feedback)
    chapters_meta = (outline.get("chapters") or [])[:7]
    if len(chapters_meta) < var["n_chapters"]:
        var = dict(var, n_chapters=max(5, len(chapters_meta)))
    outline["chapters"] = chapters_meta[:var["n_chapters"]]
    n = len(outline["chapters"])
    if n == 0:
        raise RuntimeError("outline вернул 0 глав")
    # окно объёма главы под целевые 1500-1900 слов суммарно (хук ~80 + outro ~50 учтены)
    w_lo = max(180, round(1450 / n / 10) * 10)
    w_hi = min(330, round(1850 / n / 10) * 10)
    hook = _gen_hook(topic, outline, var)
    chapters, prev_tail = [], ""
    for i in range(n):
        ch = _gen_chapter(topic, outline, i, prev_tail, var, w_lo, w_hi)
        if len(ch.get("text", "").split()) < w_lo:       # недобор → точечный до-раскрут
            ch = _expand_chapter(ch, topic, w_lo, w_hi)
        chapters.append(ch)
        prev_tail = " ".join(ch["text"].split()[-45:])   # хвост главы → нарративная связность
    outro = _gen_outro(topic, outline, prev_tail, niche)
    headings = [c["heading"] for c in chapters]
    sources = [_clean_line(s) for s in (outline.get("further_reading") or []) if _clean_line(s)][:4]
    d = _gen_description(topic, outline.get("title", topic), hook, headings)
    desc = _assemble_description(d, sources)
    if len(desc.split()) < 150:              # один локальный ретрай на длину описания
        try:
            d = _gen_description(topic, outline.get("title", topic), hook, headings, longer=True)
            desc = _assemble_description(d, sources)
        except Exception:  # noqa: BLE001
            pass
    sc = _normalize_long(niche, topic, outline, hook, chapters, outro, desc)
    try:
        sc = _polish_long(sc, var)
    except Exception:  # noqa: BLE001 — polish необязателен
        pass
    sc["total_words"] = _spoken_words(sc)
    sc["_variance"] = {k: var[k] for k in ("n_chapters", "hook_formula", "recap_chapter", "rhythm")}
    return sc


def generate_long(niche: dict, topic: str, avoid: list[str] | None = None) -> dict:
    """Сгенерировать сценарий длинной документалки по теме. Гейты: validate_long (до 3 попыток
    с фидбеком) + LLM-судья (планка QUALITY_MIN, лучший из попыток). Тема резервируется атомарно
    в topics_db (build по итогу делает commit/release — тот же протокол, что у шортсов)."""
    topic = _core.sanitize_external(topic or "")
    if not topic:
        raise ValueError("generate_long: topic обязателен")
    niche_id = niche.get("id", "")
    # антиповтор АТОМАРНО: тема известна заранее (в отличие от шортсов) → резервируем до генерации
    reserved = False
    try:
        from pipeline import topics_db
        topics_db.init()                     # build вызывает init() только в шортс-пути — здесь сами
        ok_res, match = topics_db.reserve_topic(niche_id, topic, lang=niche.get("lang", "en"))
        if ok_res or match == topic:         # совпало с нашим же резервом — не дубль
            reserved = True
        elif match:                          # реальный дубль (пустой match = сбой БД, не дубль)
            _core.log_error("script_long.duplicate", RuntimeError(f"topic ~ '{match}'"), niche=niche_id)
    except Exception:  # noqa: BLE001 — БД недоступна → дедуп просто выключен
        reserved = False

    best, fallback, feedback = None, None, ""
    for _attempt in range(MAX_LONG_ATTEMPTS):
        try:
            sc = _build_draft(niche, topic, avoid, feedback)
        except Exception as e:  # noqa: BLE001 — сбой LLM/парса, пробуем ещё раз
            _core.log_error("script_long.draft", e, niche=niche_id)
            continue
        fallback = fallback or sc
        ok, msg = validate_long(sc)
        if not ok:
            feedback = (f"\n\nPREVIOUS ATTEMPT FAILED a mechanical check: {msg}. "
                        f"Fix exactly this while keeping everything else strong.")
            continue
        sc["quality"] = _quality_score(sc)
        score = sc["quality"]["score"]
        if score >= QUALITY_MIN:
            sc["_reserved"] = reserved
            return sc
        if best is None or score > best["quality"]["score"]:
            best = sc
        feedback = (f"\n\nThe previous version scored {score}/100. Weakest axis: "
                    f"{sc['quality'].get('weakest', '')}. Fix it: {sc['quality'].get('fix', '')}. "
                    f"Keep every mechanical constraint (word counts, teases, facts).")
    sc = best or fallback
    if sc is None:
        if reserved:                          # ничего не собрали — тему освобождаем
            try:
                from pipeline import topics_db
                topics_db.release_topic(niche_id, topic)
            except Exception:  # noqa: BLE001
                pass
        raise RuntimeError("generate_long: все попытки генерации провалились")
    sc.setdefault("quality", _quality_score(sc))
    sc["_reserved"] = reserved
    return sc


# ── Чанки для озвучки (тот же контракт, что script.to_chunks) ─────────────────

def _split_even(text: str, n: int) -> list[str]:
    """Поделить текст на n частей по границам предложений, ~равного объёма слов."""
    sents = [s.strip() for s in _SENT_SPLIT.split((text or "").strip()) if s.strip()]
    if n <= 1 or len(sents) <= 1:
        return [(text or "").strip()] if (text or "").strip() else []
    per = sum(len(s.split()) for s in sents) / n
    parts, cur, cw = [], [], 0
    for s in sents:
        cur.append(s)
        cw += len(s.split())
        if cw >= per and len(parts) < n - 1:
            parts.append(" ".join(cur))
            cur, cw = [], 0
    if cur:
        parts.append(" ".join(cur))
    return parts


def to_chunks(script: dict) -> list[dict]:
    """Развернуть длинный сценарий в линейный список озвучиваемых кусков ({text, broll_query, role}) —
    структура идентична script.to_chunks(), voice.synthesize ест без изменений. Текст главы режется
    по предложениям на столько частей, сколько у главы broll_queries (свой клип на каждый кусок);
    body-чанки дополнительно несут индекс главы (для таймкодов глав в описании)."""
    chunks = []
    chapters = script.get("chapters") or []
    first_q = ""
    for ch in chapters:
        qs = [q for q in (ch.get("broll_queries") or []) if q]
        if qs:
            first_q = qs[0]
            break
    if script.get("hook"):
        chunks.append({"text": script["hook"], "broll_query": first_q, "role": "hook"})
    last_q = first_q
    for i, ch in enumerate(chapters):
        qs = [q for q in (ch.get("broll_queries") or []) if q] or ([last_q] if last_q else [""])
        parts = _split_even(ch.get("text", ""), len(qs))
        for j, part in enumerate(parts):
            chunks.append({"text": part, "broll_query": qs[min(j, len(qs) - 1)],
                           "role": "body", "chapter": i})
        last_q = qs[-1] or last_q
    if script.get("outro"):
        chunks.append({"text": script["outro"], "broll_query": last_q, "role": "outro"})
    return chunks


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
    import core
    core.load_local_secrets()
    nid = sys.argv[1] if len(sys.argv) > 1 else "history_docs"
    topic_arg = sys.argv[2] if len(sys.argv) > 2 else "The Dutch East India Company"
    niche = core.get_niche(nid)
    core.set_format(niche.get("format", "long"))
    sc = generate_long(niche, topic_arg, avoid=core.recent_topics(nid))
    print(json.dumps(sc, ensure_ascii=False, indent=2))
