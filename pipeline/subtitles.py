"""ASS-субтитры в viral-стиле shorts (ресёрч 2026).

Montserrat Black (кириллица), ALL CAPS, по 1 (короткие — по 2) слову на экран, синхронно
с озвучкой; активное/ключевое слово подсвечивается жёлтым; pop-in с лёгким overshoot;
толстая чёрная обводка (читается на любом фоне); позиция ~62% высоты (центр-нижняя треть,
выше UI площадок). Тайминги слов — из движка озвучки (EL with-timestamps / edge WordBoundary).
"""
import pathlib

FONT = "Montserrat Black"
YELLOW = "&H003DD9FF"   # #FFD93D в ASS (AABBGGRR) — подсветка ключевого слова
WHITE = "&H00FFFFFF"
CENTER_X = 540
CENTER_Y = 1190        # 1190/1920 ≈ 62% высоты — центр-нижняя треть, над кнопками платформ
SHORT = 3              # слово ≤ SHORT символов цепляем к следующему (чтобы не мелькало в одиночку)
MIN_DUR = 0.28         # минимум на экране для любого слова (anti-flicker; ~8 кадров @30)
MIN_KEY_DUR = 0.46     # ключевое/числовое слово держим дольше — глаз должен успеть прочесть (читаемость)


def _ts(t: float) -> str:
    t = max(0.0, t)
    cs = int(round(t * 100))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, c = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{c:02d}"


def _group_words(words: list[dict]) -> list[dict]:
    """1 слово на экран; короткие служебные слова цепляем к соседу. Без зазоров (anti-flicker)."""
    toks = [w for w in words if w.get("w", "").strip()]
    groups = []
    i = 0
    while i < len(toks):
        cur = [toks[i]]
        # короткое слово + следующее → одна группа (напр. «в одной», «и вот»)
        if len(toks[i]["w"].strip()) <= SHORT and i + 1 < len(toks):
            cur.append(toks[i + 1]); i += 2
        else:
            i += 1
        start = cur[0]["start"]
        end = max(cur[-1]["end"], start + MIN_DUR)
        text = " ".join(x["w"].strip() for x in cur)
        groups.append({"start": start, "end": end, "text": text})
    # тянем конец каждой группы до старта следующей — субтитр всегда на экране
    for j in range(len(groups) - 1):
        groups[j]["end"] = max(groups[j]["end"], min(groups[j + 1]["start"], groups[j]["end"] + 0.6))
    return groups


def _fontsize(text: str) -> int:
    """Крупно по умолчанию; для длинных слов уменьшаем, чтобы влезло в ширину."""
    n = len(text)
    if n <= 9:
        return 132
    if n <= 13:
        return 104
    return 82


import re as _re


def _keywords(all_words: list[dict], lang: str = "ru") -> set:
    """YAKE: вытащить СМЫСЛОВЫЕ ключевые слова из текста ролика для подсветки жёлтым
    (вместо «каждое 3-е»). Возвращает set нижне-регистровых токенов. Без GPU, RU+EN.
    Фолбэк (нет yake / сбой) — пустой set (тогда подсветка по цифрам + ритму)."""
    text = " ".join(w.get("w", "") for w in all_words).strip()
    if len(text) < 12:
        return set()
    try:
        import yake
        kx = yake.KeywordExtractor(lan=("ru" if lang == "ru" else "en"), n=1, top=12, dedupLim=0.9)
        kws = kx.extract_keywords(text)
        out = set()
        for phrase, _score in kws:
            for tok in _re.findall(r"\w+", phrase.lower()):
                if len(tok) >= 4:                 # короткие служебные не подсвечиваем
                    out.add(tok)
        return out
    except Exception:  # noqa: BLE001
        return set()


def _norm_tok(s: str) -> str:
    return _re.sub(r"\W+", "", s.lower())


def _is_accent(text: str, idx: int, keyset: set | None = None) -> bool:
    """Жёлтым подсвечиваем: числа (всегда важны) → смысловые ключевые слова (YAKE) →
    если ключевых нет (фолбэк) держим ритм каждое 3-е слово."""
    if any(c.isdigit() for c in text):
        return True
    if keyset:
        toks = [_norm_tok(t) for t in text.split()]
        # частичное совпадение покрывает падежные формы (yake даёт основу без морфологии)
        return any(t and any(t.startswith(k) or k.startswith(t) for k in keyset) for t in toks)
    return idx % 3 == 2


def _collect_words(timed_chunks: list[dict]) -> tuple[list[dict], str]:
    all_words, lang = [], "ru"
    for ch in timed_chunks:
        all_words.extend(ch.get("words", []))
        if ch.get("lang"):
            lang = ch["lang"]
    return all_words, lang


def _grouped_with_accent(timed_chunks: list[dict]) -> tuple[list[dict], set]:
    """Группы слов (как на экране) + флаг accent у каждой + гейт читаемости ключевых слов.
    Единый источник правды и для build_ass, и для accent_times (punch-in зума)."""
    all_words, lang = _collect_words(timed_chunks)
    groups = _group_words(all_words)
    keyset = _keywords(all_words, lang=lang)
    for i, g in enumerate(groups):
        g["accent"] = _is_accent(g["text"].upper(), i, keyset)
        # читаемость: ключевое/числовое слово держим на экране дольше (глаз должен успеть прочесть)
        min_d = MIN_KEY_DUR if g["accent"] else MIN_DUR
        if g["end"] - g["start"] < min_d:
            g["end"] = round(g["start"] + min_d, 3)
    return groups, keyset


def _group_phrases(words: list[dict], lo: int = 3, hi: int = 5) -> list[list[dict]]:
    """Фразы по 3-5 слов для караоке-режима (бегущая заливка \\kf, стиль Hormozi/Submagic)."""
    toks = [w for w in words if w.get("w", "").strip()]
    out, i = [], 0
    while i < len(toks):
        n = min(hi, len(toks) - i)
        if n > lo and (len(toks) - i - n) in (1, 2):   # не оставлять «хвост» из 1-2 слов
            n = max(lo, n - 1)
        out.append(toks[i:i + n]); i += n
    return out


# WrapStyle: 0 (умный автоперенос), НЕ 2 (без переноса): караоке-фразы по 3-5 слов ALL CAPS
# (кегль 92) не влезают в 1080px одной строкой и обрезались по обоим краям кадра. 0 переносит
# длинную фразу на 2 строки в пределах MarginL/R. Popin (1-2 слова) короткий → не переносится.
_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Main,{font},{fontsize},{white},{white},&H00000000,&H96000000,-1,0,0,0,100,100,0.4,0,1,6,3,5,40,40,40,1
Style: Hi,{font},{fontsize},{yellow},{yellow},&H00000000,&H96000000,-1,0,0,0,100,100,0.4,0,1,6,3,5,40,40,40,1
Style: Kara,{font},92,{yellow},{white},&H00000000,&H96000000,-1,0,0,0,100,100,0.4,0,1,6,3,5,80,80,40,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def build_ass(timed_chunks: list[dict], out_ass: pathlib.Path, accent: str = "FFD93D",
              font: str = FONT, fontsize: int = 132, mode: str = "popin") -> pathlib.Path:
    """mode='popin' — 1 слово на экран, pop-in + жёлтая подсветка смысловых (наш базовый стиль);
    mode='karaoke' — фразы 3-5 слов с бегущей заливкой \\kf (топ-стиль 2026, выше completion).
    Режим выбирается по нише (subtitle_mode) → даёт A/B пресетов субтитров."""
    header = _HEADER.format(font=font, fontsize=fontsize, white=WHITE, yellow=YELLOW)
    lines = [header]

    if mode == "karaoke":
        all_words, _ = _collect_words(timed_chunks)
        for phrase in _group_phrases(all_words):
            if not phrase:
                continue
            start, end = phrase[0]["start"], max(phrase[-1]["end"], phrase[0]["start"] + MIN_KEY_DUR)
            parts = []
            for w in phrase:
                cs = max(6, int(round((w["end"] - w["start"]) * 100)))   # длительность «пропевки» слова
                parts.append(rf"{{\kf{cs}}}{w['w'].strip().upper()} ")
            text = "".join(parts).rstrip()
            pos = rf"{{\an5\pos({CENTER_X},{CENTER_Y})}}"
            lines.append(f"Dialogue: 0,{_ts(start)},{_ts(end)},Kara,,0,0,0,,{pos}{text}\n")
    else:
        groups, _ = _grouped_with_accent(timed_chunks)
        for g in groups:
            text = g["text"].replace("\n", " ").upper()
            fs = _fontsize(text)
            hot = g["accent"]
            style = "Hi" if hot else "Main"
            # pop-in с overshoot. Ключевое (жёлтое) слово — РЕЗЧЕ и КРУПНЕЕ (active-word emphasis,
            # стиль Submagic): больший overshoot 60→122→106% — глаз цепляется за смысловое слово.
            if hot:
                anim = (rf"{{\an5\pos({CENTER_X},{CENTER_Y})\fs{fs}"
                        rf"\fscx60\fscy60\t(0,80,\fscx122\fscy122)\t(80,170,\fscx106\fscy106)}}")
            else:
                anim = (rf"{{\an5\pos({CENTER_X},{CENTER_Y})\fs{fs}"
                        rf"\fscx72\fscy72\t(0,90,\fscx112\fscy112)\t(90,150,\fscx100\fscy100)}}")
            lines.append(
                f"Dialogue: 0,{_ts(g['start'])},{_ts(g['end'])},{style},,0,0,0,,{anim}{text}\n"
            )

    out_ass.parent.mkdir(parents=True, exist_ok=True)
    out_ass.write_text("".join(lines), encoding="utf-8")
    return out_ass


# ──────────────────────────── SRT для long-form (НЕ вжигается) ────────────────────────────
# Документалка 16:9: субтитры отдаются YouTube отдельным .srt файлом при загрузке.
SRT_MAX_LINE = 42       # максимум символов в строке (стандарт читаемости субтитров)
SRT_MAX_LINES = 2       # 1-2 строки на титр
SRT_MIN_DUR = 1.0       # титр держим на экране минимум 1с
SRT_MAX_DUR = 6.0       # и максимум 6с
_SRT_GAP = 0.6          # пауза в речи ≥ этой — граница фразы
_PHRASE_END = _re.compile(r"[.!?…]$|[,;:—-]$")


def _srt_ts(t: float) -> str:
    t = max(0.0, t)
    ms = int(round(t * 1000))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _wrap_lines(text: str, width: int = SRT_MAX_LINE) -> list[str]:
    """Жадный перенос по словам в строки ≤ width (одиночное сверхдлинное слово не режем)."""
    lines, cur = [], ""
    for w in text.split():
        cand = (cur + " " + w).strip()
        if cur and len(cand) > width:
            lines.append(cur)
            cur = w
        else:
            cur = cand
    if cur:
        lines.append(cur)
    return lines


def _fits_caption(text: str) -> bool:
    """Влезает ли текст в SRT_MAX_LINES строк по SRT_MAX_LINE (проверка реальным переносом)."""
    return len(_wrap_lines(text)) <= SRT_MAX_LINES


def _srt_cues(words: list[dict]) -> list[dict]:
    """Слова с таймингами → титры: рвём по границам фраз (пунктуация/пауза), не переполняем
    2 строки по SRT_MAX_LINE, длительность титра держим в [SRT_MIN_DUR, SRT_MAX_DUR]."""
    cues, cur = [], []

    def flush():
        if cur:
            cues.append({"start": cur[0]["start"], "end": cur[-1]["end"],
                         "text": " ".join(x["w"].strip() for x in cur)})
            cur.clear()

    for w in words:
        tok = w.get("w", "").strip()
        if not tok:
            continue
        cand = " ".join([x["w"].strip() for x in cur] + [tok])
        if cur and (not _fits_caption(cand)                         # не влезает в 2 строки
                    or w["start"] - cur[-1]["end"] >= _SRT_GAP      # пауза в речи
                    or w["end"] - cur[0]["start"] > SRT_MAX_DUR):   # титр затянулся
            flush()
        cur.append(w)
        # конец предложения/фразы — естественная граница титра (если он уже не мигнёт)
        if _PHRASE_END.search(tok) and cur[-1]["end"] - cur[0]["start"] >= SRT_MIN_DUR:
            flush()
    flush()

    # слияние коротышей: титр < SRT_MIN_DUR клеим к следующему, пока влезает по тексту и времени
    merged = []
    for c in cues:
        if (merged and merged[-1]["end"] - merged[-1]["start"] < SRT_MIN_DUR
                and _fits_caption(merged[-1]["text"] + " " + c["text"])
                and c["end"] - merged[-1]["start"] <= SRT_MAX_DUR
                and c["start"] - merged[-1]["end"] < _SRT_GAP):
            merged[-1]["text"] += " " + c["text"]
            merged[-1]["end"] = c["end"]
        else:
            merged.append(dict(c))

    # финальная нормализация длительности: минимум — тянем до старта следующего, максимум — режем
    for j, c in enumerate(merged):
        nxt = merged[j + 1]["start"] if j + 1 < len(merged) else None
        if c["end"] - c["start"] < SRT_MIN_DUR:
            c["end"] = c["start"] + SRT_MIN_DUR
            if nxt is not None:
                c["end"] = min(c["end"], nxt)
        if c["end"] - c["start"] > SRT_MAX_DUR:
            c["end"] = c["start"] + SRT_MAX_DUR
        c["end"] = max(c["end"], c["start"] + 0.3)   # страховка от нулевой длительности

    # монотонность блоков: пословные тайминги Groq могут перекрываться на стыках чанков →
    # старт титра раньше конца предыдущего = формально невалидный SRT (перекрытие блоков)
    prev_end = 0.0
    for c in merged:
        if c["start"] < prev_end:
            c["start"] = prev_end
        if c["end"] < c["start"] + 0.3:
            c["end"] = c["start"] + 0.3
        prev_end = c["end"]
    return merged


def build_srt(timed_chunks: list[dict], out_srt: pathlib.Path) -> pathlib.Path:
    """Стандартный SRT из пословных таймингов (long-form, НЕ вжигается в кадр).
    Титры по 1-2 строки ≤ ~42 символов, разрез по границам фраз, длительность 1-6с.
    Чистая функция: без сети, без core."""
    all_words, _ = _collect_words(timed_chunks)
    cues = _srt_cues([w for w in all_words if w.get("w", "").strip()])
    blocks = []
    for i, c in enumerate(cues, 1):
        text = "\n".join(_wrap_lines(c["text"])[:SRT_MAX_LINES])
        blocks.append(f"{i}\n{_srt_ts(c['start'])} --> {_srt_ts(c['end'])}\n{text}\n")
    out_srt = pathlib.Path(out_srt)
    out_srt.parent.mkdir(parents=True, exist_ok=True)
    out_srt.write_text("\n".join(blocks), encoding="utf-8")
    return out_srt


def accent_times(timed_chunks: list[dict]) -> list[dict]:
    """Таймкоды акцентных (ключевых/числовых) слов → для punch-in зума в assemble.
    Возвращает [{"t": старт_слова_сек, "dur": длительность_импульса}] на финальном таймлайне."""
    groups, _ = _grouped_with_accent(timed_chunks)
    out = []
    for g in groups:
        if g.get("accent"):
            out.append({"t": round(g["start"], 3),
                        "dur": round(min(0.25, max(0.12, g["end"] - g["start"])), 3)})
    return out
