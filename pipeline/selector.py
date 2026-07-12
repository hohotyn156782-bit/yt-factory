"""Селектор тем (этап 3): тренды парсера → готовые темы видео.

Берёт кандидатов из parser.gather(), скорит, отсекает дубли и недавнее, и через
LLM-каскад выбирает N самых перспективных, переформулируя каждый в КОНКРЕТНУЮ
цепляющую тему видео под нишу (а не сухой новостной заголовок). Для format=long
(документалки) промпт-продюсер миксует вечнозелёный topic_bank ниши (неиспользованные
записи) с трендовыми углами (годовщины, виральные исторические треды). Политику/чернуху/
демонетизируемое отсеивает. Если LLM недоступен — фолбэк: long → банк тем, short → топ парсера.
"""
import json
import re

import sys, pathlib  # noqa: E401
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import core  # noqa: E402
from pipeline import parser, llm  # noqa: E402


def _clean(t: str) -> str:
    return re.sub(r"\s+", " ", str(t or "")).strip().strip('"«»')


_INJECT = re.compile(r"(?i)(ignore (all |the )?previous|disregard (above|previous)|system\s*:|"
                     r"ты теперь|забудь (все )?(инструкции|предыдущ)|new instructions|act as|"
                     r"<\|?(system|im_start|im_end)\|?>)")


def _sanitize(t: str) -> str:
    """Очистить текст из ПАРСЕРА (чужой контент) перед подачей в LLM — анти-prompt-injection.
    Тонкая обёртка над core.sanitize_external (единый паттерн для всего пайплайна) + локальная
    доп.строгость: наш _INJECT ловит ru-инъекции/«act as»/«new instructions», которых нет в core."""
    t = core.sanitize_external(_clean(t))   # базовая очистка (общий паттерн)
    t = _INJECT.sub("[…]", t)               # поверх: дополнительные паттерны перехвата (строже core)
    t = re.sub(r"[`{}<>\\]", "", t)          # спецсимволы разметки/скобки
    return t[:140]


def pick_topics(niche: dict, n: int = 2, recent: list[str] | None = None) -> list[str]:
    is_ru = niche.get("lang", "en") == "ru"
    is_long = niche.get("format") == "long"
    cands = []
    try:
        cands = parser.gather(niche)
    except Exception:  # noqa: BLE001 — источники не должны ронять пайплайн
        cands = []
    top = sorted(cands, key=lambda x: -x.get("weight", 0))[:28]
    trends_lines = [f"- [{c['source']}] {_sanitize(c['title'])}" for c in top]

    # H3: горячие подтемы из ЛЕГАЛЬНОГО вираль-брифа ниши (метаданные чужих топ-Shorts) как
    # доп.кандидаты с пометкой источника. Гейт CF_VIRAL (как в script.py), мягко, не роняет селектор.
    import os
    if os.environ.get("CF_VIRAL", "1") != "0":
        try:
            from pipeline import heatmap
            vq = (niche.get("broll_hint", "") or niche.get("title", "")).split(",")[0].strip()
            brief = heatmap.viral_brief(vq, lang=niche.get("lang", "ru")) if vq else {}
            for st in (brief.get("hot_subtopics") or [])[:8]:
                st = _sanitize(st)
                if st:
                    trends_lines.append(f"- [viral_brief] {st}")
        except Exception:  # noqa: BLE001 — доп.кандидаты необязательны
            pass

    trends_block = "\n".join(trends_lines) or "(no live trends available — invent strong evergreen topics for the niche yourself)"

    # #15 дедуп против durable topics_db: подмешиваем уже выпущенные/зарезервированные темы
    # ниши за 60 дней к списку избегания → LLM-продюсер сразу обходит дубли (меньше коллизий
    # reserve_topic в generate(), больше attempt-итераций на докрутку хука/удержания/петли).
    # Fallback-safe: любой сбой импорта/вызова → текущее поведение (только переданный recent).
    avoid = list(recent or [])
    try:
        from pipeline import topics_db
        avoid += topics_db.recent_titles(niche=niche.get("id"), days=60) or []
    except Exception:  # noqa: BLE001 — durable-дедуп необязателен, не роняет селектор
        pass

    recent_block = ""
    if avoid:
        seen, uniq = set(), []
        for r in avoid:
            r = _clean(r)[:60]
            k = r.lower()
            if r and k not in seen:
                seen.add(k)
                uniq.append(r)
        if uniq:
            recent_block = "\nDo NOT repeat these recent topics: " + " | ".join(uniq[:25])

    # long-form: банк вечнозелёных тем ниши (topic_bank), ещё НЕ использованных (сверка с avoid).
    # LLM-продюсеру даём выборку из банка + тренды → он миксует вечнозелёное с трендовыми углами.
    bank_unused: list[str] = []
    if is_long:
        avoid_low = [a.lower() for a in (a for a in map(_clean, avoid) if a)]
        for t in (niche.get("topic_bank") or []):
            t = _clean(t)
            tl = t.lower()
            if t and not any(a in tl or tl[:60] in a for a in avoid_low):
                bank_unused.append(t)

    lang_word = "Russian" if is_ru else "English"
    if is_long:
        system = (
            f"You are the executive producer of a YouTube history documentary channel \"{niche.get('title')}\" — "
            f"{niche.get('topic_brief')}\n"
            f"You are given the channel's evergreen TOPIC BANK (unused entries) and fresh trend signals. "
            f"Pick the {n} most promising DOCUMENTARY topics for views and subscriber growth. Every topic must be "
            f"evergreen, searchable, and have a specific angle (e.g. \"How X actually worked\", \"Why X collapsed\", "
            f"\"Who really got rich from X\") — never a dry news headline. Prefer topic-bank entries; use a trend "
            f"signal only when it genuinely fits history (an anniversary, a viral history thread) and reshape it "
            f"into an evergreen documentary angle. "
            f"AVOID: politics, recent tragedies, NSFW, medical or financial advice, living private individuals, "
            f"anything that gets demonetized on YouTube. "
            f"Topics must be in {lang_word}.{recent_block}\n"
            f'Return STRICT JSON: {{"topics": [{", ".join(["\"...\""] * n)}]}} — exactly {n} topics.'
        )
        parts = []
        if bank_unused:
            import random
            sample = random.sample(bank_unused, min(12, len(bank_unused)))
            parts.append("Evergreen topic bank (unused):\n" + "\n".join(f"- {t}" for t in sample))
        parts.append("Fresh trend signals:\n" + trends_block)
        user = "\n\n".join(parts)
    else:
        system = (
            f"You are a producer of viral short videos (Shorts/TikTok/Reels) in the \"{niche.get('title')}\" niche — "
            f"{niche.get('topic_brief')}\n"
            f"You are given fresh trends/news. Pick the {n} MOST promising for views and subscriber growth and "
            f"rephrase EACH into a specific, hooky VIDEO TOPIC for the niche (not a news headline — an angle that "
            f"will hit). Favor curiosity, value, and emotion. "
            f"AVOID: politics, tragedies/shock content, NSFW, narrow local news, anything that gets demonetized. "
            f"Topics must be in {lang_word}.{recent_block}\n"
            f'Return STRICT JSON: {{"topics": [{", ".join(["\"...\""] * n)}]}} — exactly {n} topics.'
        )
        user = "Fresh trends:\n" + trends_block
    try:
        raw = llm.chat(system, user, json_mode=True, max_tokens=500)
        topics = [_clean(t) for t in (json.loads(_extract(raw)).get("topics") or []) if _clean(t)]
        if topics:
            return topics[:n]
    except Exception:  # noqa: BLE001
        pass
    # фолбэк без LLM: long → неиспользованные темы из банка (свой контент, чистые);
    # иначе топовые заголовки парсера как темы (чужой контент → _sanitize, не _clean: анти-injection)
    if is_long and bank_unused:
        return bank_unused[:n]
    return [_sanitize(c["title"]) for c in top[:n] if _sanitize(c["title"])] or [""]


def _extract(raw: str) -> str:
    try:
        json.loads(raw)
        return raw
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        return m.group(0) if m else '{"topics": []}'


if __name__ == "__main__":
    core.load_local_secrets()
    nid = sys.argv[1] if len(sys.argv) > 1 else "history_docs"
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    topics = pick_topics(core.get_niche(nid), n=n)
    print(f"[{nid}] выбранные темы:")
    for t in topics:
        print("  •", t)
