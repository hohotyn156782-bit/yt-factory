"""Авто-обложки (thumbnail): вертикальные для Shorts + 16:9 для long-form YouTube.

Shorts (донорский путь, без изменений): кадр/AI-фон cover-crop до core.W×core.H,
тёмный градиент снизу, ALL-CAPS заголовок крупным жирным шрифтом (Montserrat Black)
с толстой чёрной обводкой и акцент-цветом ниши.

Long-form (fmt=="long" или core.ACTIVE_FORMAT=="long"): канва 1280×720 (спека YouTube,
JPEG <2МБ). Композиция по CTR-ресёрчу 2026: ОДИН доминантный объект (лучший кадр видео
или FLUX-картинка) занимает правые ~60%, слева тёмная градиентная зона с ALL-CAPS
текстом ≤5 слов, толстая обводка, один акцент-цвет из палитры ниши, опционально
простая стрелка/круг (Pillow). Правило «3 секунд»: огромный кегль, 2-3 цвета,
без визуального мусора. Любой сбой → фолбэк на сплошной фон бренд-цвета, None при
полном провале (с core.log_error).

#10: По умолчанию фон обложки генерится AI (NVIDIA FLUX → Pollinations, каскад imagegen) —
яркий кликбейт-постер ради CTR; при исчерпании квоты/оффлайне фолбэк на лучший кадр видео.
Отключить AI-фон: env THUMB_AI_BG=0.

Pillow импортируем ЛЕНИВО внутри функций (как договорено в проекте).
"""
import os
import re
import sys
import hashlib
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import core  # noqa: E402

# Бренд {P} — акцент по умолчанию, если у ниши нет палитры.
BRAND_ACCENT = "#3DDC97"
BRAND_DARK = "#080A09"

# Спека YouTube для обложек long-form: ровно 1280×720, JPEG, файл <2МБ.
LONG_W, LONG_H = 1280, 720
THUMB_MAX_BYTES = 2 * 1024 * 1024


def _font_path() -> str:
    """Montserrat Black из ассетов проекта (кириллица), фолбэк — DejaVuSans-Bold."""
    f = core.ASSETS_DIR / "fonts" / "Montserrat-Black.ttf"
    if f.exists():
        return str(f)
    # запасные варианты внутри проекта, потом системный жирный
    for alt in ("Montserrat-ExtraBold.ttf", "Montserrat-Bold.ttf"):
        p = core.ASSETS_DIR / "fonts" / alt
        if p.exists():
            return str(p)
    return core.FONT_BOLD


def _accent_from_niche(niche: dict | None) -> str:
    """Акцент-цвет: первая палитра ниши (пара [тёмный, акцент]) → бренд-зелёный."""
    if niche:
        pal = niche.get("palette") or []
        try:
            hexc = pal[0][1]                       # [[dark, accent], ...]
            hexc = str(hexc).lstrip("#")
            if len(hexc) == 6:
                return "#" + hexc
        except (IndexError, TypeError, KeyError):
            pass
    return BRAND_ACCENT


def _dark_from_niche(niche: dict | None) -> str:
    """Тёмный цвет ниши: первый элемент первой пары палитры → бренд-тёмный."""
    if niche:
        pal = niche.get("palette") or []
        try:
            hexc = str(pal[0][0]).lstrip("#")      # [[dark, accent], ...]
            if len(hexc) == 6:
                return "#" + hexc
        except (IndexError, TypeError, KeyError):
            pass
    return BRAND_DARK


def _extract_frame(video_path: str, frame_t: float | None, dst: pathlib.Path) -> bool:
    """Вытащить один кадр в момент frame_t (по умолчанию ~15% длительности) в PNG.
    True при успехе непустого файла."""
    dur = core.media_duration(video_path)
    if frame_t is None:
        frame_t = max(0.3, dur * 0.15) if dur > 0 else 1.0
    # не вылезти за конец ролика
    if dur > 0:
        frame_t = min(frame_t, max(0.1, dur - 0.2))
    try:
        # -ss перед -i = быстрый seek; -frames:v 1 = ровно один кадр
        core.run([
            "ffmpeg", "-y", "-ss", f"{frame_t:.3f}", "-i", str(video_path),
            "-frames:v", "1", "-q:v", "2", str(dst),
        ])
    except Exception as e:  # noqa: BLE001 — фолбэк на сплошной фон выше
        core.log_error("thumbnail._extract_frame", e, video=str(video_path))
        return False
    return dst.exists() and dst.stat().st_size > 0


def _frame_score(img) -> float:
    """#8: оценка «обложечности» кадра только через Pillow (без numpy/opencv).
    Score = резкость · яркость-в-безопасном-диапазоне · контраст.
      • резкость   — дисперсия градиента (|сосед-сосед|) уменьшенной L-копии (прокси Лапласа);
      • яркость    — штраф за пере/недо-экспозицию (целимся в средний серый ~115);
      • контраст   — СКО гистограммы яркости (разброс тонов).
    Чем выше — тем «сочнее»/чётче кадр. Все компоненты нормированы в ~[0..1]."""
    from PIL import Image, ImageFilter  # ленивый импорт
    g = img.convert("L")
    # ужимаем до ~256px по длинной стороне — быстрый и устойчивый к шуму замер
    long = max(g.width, g.height)
    if long > 256:
        s = 256 / long
        g = g.resize((max(1, round(g.width * s)), max(1, round(g.height * s))), Image.BILINEAR)

    # --- резкость: дисперсия отклика edge-фильтра (прокси дисперсии Лапласа) ---
    edges = g.filter(ImageFilter.FIND_EDGES)
    eh = edges.histogram()
    n = sum(eh) or 1
    mean_e = sum(i * c for i, c in enumerate(eh)) / n
    var_e = sum(((i - mean_e) ** 2) * c for i, c in enumerate(eh)) / n
    sharp = min(1.0, var_e / 2000.0)          # ~2000 = «достаточно резкий» потолок

    # --- яркость: средняя по гистограмме L; штраф за тьму/пересвет ---
    h = g.histogram()
    nb = sum(h) or 1
    mean_b = sum(i * c for i, c in enumerate(h)) / nb
    # «безопасный» диапазон ~[60..190]; вне — резко падает (гаусс вокруг 115)
    bright = pow(2.718281828, -((mean_b - 115.0) ** 2) / (2 * (55.0 ** 2)))

    # --- контраст: СКО яркости по гистограмме (нормируем к ~64) ---
    var_b = sum(((i - mean_b) ** 2) * c for i, c in enumerate(h)) / nb
    std_b = var_b ** 0.5
    contrast = min(1.0, std_b / 64.0)

    # резкость — главный фактор; яркость как множитель режет «мусорные» кадры
    return (sharp * 0.6 + contrast * 0.4) * (0.35 + 0.65 * bright)


def _best_frame(video_path: str, dst: pathlib.Path) -> bool:
    """#8: вытащить 4-5 кадров-кандидатов (12/20/35/55/75% длительности), оценить каждый
    через _frame_score и оставить лучший как dst. Заменяет «лотерею» dur*0.15.
    Фолбэк: при любом сбое — обычный _extract_frame(None) (тот самый ~15%-кадр)."""
    try:
        from PIL import Image  # ленивый импорт; нет PIL → фолбэк ниже
    except Exception as e:  # noqa: BLE001
        core.log_error("thumbnail._best_frame(PIL import)", e)
        return _extract_frame(video_path, None, dst)

    dur = core.media_duration(video_path)
    if dur <= 0:
        return _extract_frame(video_path, None, dst)

    fracs = (0.12, 0.20, 0.35, 0.55, 0.75)
    best_score, best_path = -1.0, None
    cand_dir = dst.parent
    cands: list[pathlib.Path] = []
    try:
        for i, fr in enumerate(fracs):
            t = min(max(0.3, dur * fr), max(0.1, dur - 0.2))
            cp = cand_dir / (dst.stem + f"_c{i}.png")
            if not _extract_frame(video_path, t, cp):
                continue
            cands.append(cp)
            try:
                with Image.open(cp) as im:
                    sc = _frame_score(im)
            except Exception as e:  # noqa: BLE001 — битый кандидат пропускаем
                core.log_error("thumbnail._best_frame(score)", e, frame=cp.name)
                continue
            if sc > best_score:
                best_score, best_path = sc, cp

        if best_path is None:                 # ни один кандидат не оценился → фолбэк
            for cp in cands:
                cp.unlink(missing_ok=True)
            return _extract_frame(video_path, None, dst)

        # лучший кандидат → dst (заменяя при необходимости), остальные чистим
        dst.unlink(missing_ok=True)
        best_path.replace(dst)
        for cp in cands:
            if cp != best_path:
                cp.unlink(missing_ok=True)
        core.log(f"обложка: выбран лучший кадр (score={best_score:.3f})", thumb=dst.name)
        return dst.exists() and dst.stat().st_size > 0
    except Exception as e:  # noqa: BLE001
        core.log_error("thumbnail._best_frame", e, video=str(video_path))
        for cp in cands:
            cp.unlink(missing_ok=True)
        return _extract_frame(video_path, None, dst)


def _cover_crop(base, W: int, H: int):
    """Масштаб «на покрытие» + центр-кроп до W×H (PIL Image)."""
    from PIL import Image  # ленивый импорт
    scale = max(W / base.width, H / base.height)
    nw, nh = max(1, round(base.width * scale)), max(1, round(base.height * scale))
    base = base.resize((nw, nh), Image.LANCZOS)
    left, top = (nw - W) // 2, (nh - H) // 2
    return base.crop((left, top, left + W, top + H))


def _thumb_bg_prompt(niche: dict | None, query: str = "", fmt: str = "short") -> str:
    """#10: EN-промпт постер-фона обложки — яркий драматичный кадр по теме ниши.
    Субъект берём из query → иначе первый намёк broll_hint ниши. Для long — ОДИН
    доминантный объект в центре на тёмном фоне (композиция «объект справа» делается
    кропом при компоновке)."""
    subj = (query or "").strip()
    if not subj and niche:
        hints = [h.strip() for h in (niche.get("broll_hint") or "").split(",") if h.strip()]
        subj = ", ".join(hints[:3])               # 3 намёка = богаче сцена, чем одно слово
    subj = subj or "dramatic abstract scene"
    if fmt == "long":
        return (f"dramatic cinematic documentary poster, {subj}, one single dominant focal subject "
                f"centered in frame, glossy magazine-cover style, rich moody colors, strong cinematic "
                f"rim light and glow, dark shadowy background, deep depth of field, "
                f"no text, no letters, no captions, no watermark, ultra detailed, photographic")
    return (f"eye-catching dramatic poster, {subj}, vivid bold concrete focal subject filling the "
            f"upper two thirds, glossy magazine-cover style, rich saturated colors, strong cinematic "
            f"rim light and glow, deep depth of field, the lower third fades into dark shadow, "
            f"no text, no letters, no captions, no watermark, ultra detailed, photographic, 9:16 vertical")


def _ai_background(niche: dict | None, query: str, dst: pathlib.Path, seed: int,
                   fmt: str = "short") -> bool:
    """#10: сгенерировать AI-фон обложки через NVIDIA FLUX (каскад imagegen.generate_raw).
    True при успехе. Любой сбой/нет ключа/исчерпана квота → False (выше — фолбэк на кадр)."""
    try:
        try:
            from pipeline import imagegen as ig
        except ImportError:
            import imagegen as ig  # запуск из каталога pipeline/
    except Exception as e:  # noqa: BLE001
        core.log_error("thumbnail._ai_background(import)", e)
        return False
    try:
        res = ig.generate_raw(_thumb_bg_prompt(niche, query, fmt=fmt), dst, seed=seed)
        return bool(res) and dst.exists() and dst.stat().st_size > 5000
    except Exception as e:  # noqa: BLE001
        core.log_error("thumbnail._ai_background", e)
        return False


def make_thumbnail(video_path: str, title: str, out_path: pathlib.Path,
                   niche: dict | None = None, frame_t: float | None = None,
                   ai_bg: bool = True, bg_query: str = "", seed: int = 0,
                   fmt: str | None = None) -> pathlib.Path | None:
    """Сгенерировать обложку: AI-постер-фон (NVIDIA FLUX) или кадр из видео + заголовок поверх.
    ai_bg=True (по умолч.) — фон генерится FLUX'ом; при сбое/квоте — фолбэк на лучший кадр.
    fmt: "long" → 16:9 канва 1280×720 (спека YouTube); "short" → вертикаль core.FORMATS;
    None → core.ACTIVE_FORMAT на момент вызова. Вернуть путь или None."""
    try:
        from PIL import Image, ImageDraw, ImageFont, ImageColor  # ленивый импорт
    except Exception as e:  # noqa: BLE001
        core.log_error("thumbnail.make_thumbnail(PIL import)", e)
        return None

    fmt = (fmt or core.ACTIVE_FORMAT or "long")
    if fmt == "long":
        W, H = LONG_W, LONG_H                     # обложка long — всегда 1280×720, не размер рендера
    else:
        W, H = core.FORMATS.get(fmt, (core.W, core.H))
    out_path = pathlib.Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    accent = _accent_from_niche(niche)
    font_file = _font_path()

    try:
        # 1) База обложки (приоритет): AI-постер-фон (NVIDIA FLUX) → лучший кадр из видео →
        #    сплошной бренд-фон. AI = яркий кликбейт-кадр (CTR); кадр — надёжный фолбэк при
        #    исчерпании квоты/оффлайне. #8: при frame_t=None берём ЛУЧШИЙ кадр из кандидатов.
        #    Базу храним НЕкропнутой: long и short кропают её по-разному при компоновке.
        img = None
        base_kind = "solid"
        tmp = out_path.with_name(out_path.stem + "_frame.png")
        if ai_bg:
            base_seed = seed or int(hashlib.md5(
                ((title or "") + str((niche or {}).get("id", ""))).encode()).hexdigest()[:6], 16)
            n_ab = max(1, min(3, int(os.environ.get("THUMB_AB", "2") or 2)))   # A/B: N фонов → лучший
            best_sc = -1.0
            for k in range(n_ab):
                cand = out_path.with_name(out_path.stem + f"_ai{k}.png")
                if _ai_background(niche, bg_query, cand, (base_seed + k * 1009) & 0x7fffffff, fmt=fmt):
                    try:
                        im = Image.open(cand).convert("RGB")
                        scv = _frame_score(im)               # «сочность»: резкость+контраст+яркость
                        if scv > best_sc:
                            best_sc, img, base_kind = scv, im, "ai"
                    except Exception as e:  # noqa: BLE001
                        core.log_error("thumbnail.ai_open", e)
                cand.unlink(missing_ok=True)
            if base_kind == "ai":
                core.log(f"обложка A/B: лучший из {n_ab} (score={best_sc:.3f})", level="debug")
        if img is None:
            got = _best_frame(video_path, tmp) if frame_t is None \
                else _extract_frame(video_path, frame_t, tmp)
            if got:
                img = Image.open(tmp).convert("RGB")
                base_kind = "frame"
            try:
                tmp.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
        if img is None:
            img = Image.new("RGB", (W, H), BRAND_DARK)

        # 2) Компоновка: long — свой 16:9 путь (объект справа, текст слева); short — донорский.
        if fmt == "long":
            res = _compose_long(img, title, niche, accent, font_file, out_path)
            if res:
                core.log(f"обложка готова ({base_kind}, long 16:9): {out_path.name}",
                         thumb=str(out_path))
            return res

        img = _cover_crop(img, W, H)
        draw = ImageDraw.Draw(img)

        # 2a) Затемнение нижней трети градиентом (сверху прозрачно → снизу почти чёрно):
        #     текст внизу станет читаемым на любом фоне.
        grad_top = int(H * 0.55)                  # начинаем темнить с ~55% высоты
        grad_h = H - grad_top
        overlay = Image.new("RGBA", (W, grad_h), (0, 0, 0, 0))
        odraw = ImageDraw.Draw(overlay)
        for y in range(grad_h):
            a = int(245 * (y / max(1, grad_h - 1)) ** 1.4)   # 0 → ~245, нелинейно (мягче сверху)
            odraw.line([(0, y), (W, y)], fill=(0, 0, 0, a))
        img.paste(overlay, (0, grad_top), overlay)
        draw = ImageDraw.Draw(img)

        # 2b) Опц. маленькая плашка ниши сверху (название ниши акцент-цветом на тёмной полоске).
        try:
            tag = (niche or {}).get("title", "") if niche else ""
            tag = (tag or "").strip().upper()
            if tag:
                if len(tag) > 26:
                    tag = tag[:25].rstrip() + "…"
                tag_font = ImageFont.truetype(font_file, 40)
                tb = draw.textbbox((0, 0), tag, font=tag_font)
                tw, th = tb[2] - tb[0], tb[3] - tb[1]
                pad_x, pad_y = 34, 20
                bx0, by0 = 60, 70
                bx1, by1 = bx0 + tw + pad_x * 2, by0 + th + pad_y * 2
                draw.rounded_rectangle([bx0, by0, bx1, by1], radius=18, fill=(8, 10, 9, 235))
                draw.text((bx0 + pad_x, by0 + pad_y - tb[1]), tag, font=tag_font, fill=accent)
        except Exception as e:  # noqa: BLE001 — плашка не обязательна
            core.log_error("thumbnail.tag", e)

        # 2c) Заголовок ALL CAPS внизу: подбираем размер шрифта так, чтобы 1-3 строки
        #     влезли по ширине и не залезли в нижнюю четверть (UI площадок).
        text = (title or "").strip().upper() or "WATCH TILL THE END"
        max_w = W - 120                           # поля по 60px слева/справа
        max_text_h = int(H * 0.34)                # высота блока заголовка
        # #9: текст теперь короткий хук (≤5 слов) → агрессивнее держим КРУПНЫЙ кегль,
        #     минимум 90px вместо 50px (лучше усечь хвост слов, чем мельчить).
        MIN_SIZE = 90
        stroke = 0
        chosen = None
        for size in range(150, MIN_SIZE - 1, -6):
            font = ImageFont.truetype(font_file, size)
            stroke = max(4, size // 16)           # толстая обводка, масштабируется с кеглем
            lines = _wrap(draw, text, font, max_w, stroke)
            if len(lines) > 3:
                continue
            line_h = _line_height(draw, font, stroke)
            block_h = line_h * len(lines)
            widest = max((_text_w(draw, ln, font, stroke) for ln in lines), default=0)
            if widest <= max_w and block_h <= max_text_h:
                chosen = (font, lines, line_h, block_h)
                break
        if chosen is None:                        # не влез даже на минимуме → держим кегль,
            font = ImageFont.truetype(font_file, MIN_SIZE)   #   усекаем слова, а не мельчим
            stroke = max(4, MIN_SIZE // 16)
            lines = _wrap(draw, text, font, max_w, stroke)[:3]
            line_h = _line_height(draw, font, stroke)
            chosen = (font, lines, line_h, line_h * len(lines))
        font, lines, line_h, block_h = chosen

        # 3) Рисуем заголовок: нижний край блока ~88% высоты (над кнопками платформ),
        #    белый текст, чёрная толстая обводка, последняя строка — акцент-цветом (хук).
        bottom_y = int(H * 0.88)
        y = bottom_y - block_h
        for i, ln in enumerate(lines):
            lw = _text_w(draw, ln, font, stroke)
            x = (W - lw) // 2
            fill = accent if (i == len(lines) - 1 and len(lines) > 1) else "#FFFFFF"
            draw.text((x, y), ln, font=font, fill=fill,
                      stroke_width=stroke, stroke_fill="black")
            y += line_h

        # 4) JPEG с капом размера (лимит YouTube <2МБ; для вертикалей обычно и так меньше)
        _save_jpeg_capped(img, out_path, start_q=88)
        core.log(f"обложка готова ({base_kind}): {out_path.name}", thumb=str(out_path))
        return out_path
    except Exception as e:  # noqa: BLE001
        core.log_error("thumbnail.make_thumbnail", e, video=str(video_path))
        return None


# ──────────────────────────── Long-form 16:9 (1280×720) ────────────────────────────

def _save_jpeg_capped(img, out_path: pathlib.Path, start_q: int = 90) -> pathlib.Path:
    """Сохранить JPEG не тяжелее THUMB_MAX_BYTES (лимит YouTube 2МБ): quality вниз по шагу 10."""
    q = start_q
    while True:
        img.convert("RGB").save(out_path, "JPEG", quality=q, optimize=True)
        if out_path.stat().st_size <= THUMB_MAX_BYTES or q <= 60:
            return out_path
        q -= 10


def _draw_accent_shape(draw, accent: str, W: int, H: int, img_x0: int, text_bottom: int) -> None:
    """Опц. простой акцент (CTR-ресёрч: одна простая фигура, не мусор): стрелка от текстовой
    зоны к объекту или круг вокруг объекта. env THUMB_ACCENT=arrow|circle|none (дефолт arrow)."""
    kind = os.environ.get("THUMB_ACCENT", "arrow").strip().lower()
    if kind in ("0", "none", "off", "no", ""):
        return
    try:
        if kind == "circle":
            cx = img_x0 + (W - img_x0) // 2
            cy = int(H * 0.46)
            r = int(H * 0.30)
            draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=accent, width=12)
        else:                                     # arrow: короткая толстая, указывает на объект
            y = min(H - 80, text_bottom + 56)
            x0, x1 = 72, int(W * 0.30)
            draw.line([(x0, y), (x1, y)], fill=accent, width=16)
            draw.polygon([(x1 + 46, y), (x1 - 4, y - 32), (x1 - 4, y + 32)], fill=accent)
    except Exception as e:  # noqa: BLE001 — акцент не обязателен
        core.log_error("thumbnail.accent_shape", e)


def _compose_long(src, text: str, niche: dict | None, accent: str, font_file: str,
                  out_path: pathlib.Path) -> pathlib.Path | None:
    """16:9 обложка long-form: канва 1280×720. Доминантный объект (src) занимает правые ~60%
    с мягким левым краем (alpha-ramp — без шва), слева тёмная зона + градиент читабельности,
    ALL-CAPS текст ≤5 слов Montserrat Black с толстой обводкой, последняя строка / подчёркивание
    акцент-цветом ниши, опц. стрелка/круг. Правило 3 секунд: огромный кегль, 2-3 цвета."""
    from PIL import Image, ImageDraw, ImageFont, ImageColor  # ленивый импорт
    W, H = LONG_W, LONG_H
    dark = _dark_from_niche(niche)
    canvas = Image.new("RGB", (W, H), dark)

    # объект справа: картинка от ~32% ширины до края (после градиента визуально доминирует
    # правые ~60%); вертикальные AI-фоны (768×1344) кропаются в регион 870×720 без апскейла
    img_x0 = int(W * 0.32)
    region_w = W - img_x0
    region = _cover_crop(src, region_w, H)
    fade = min(200, region_w // 3)                # мягкий левый край — растворение в тёмной зоне
    mask = Image.new("L", (region_w, H), 255)
    md = ImageDraw.Draw(mask)
    for x in range(fade):
        md.line([(x, 0), (x, H)], fill=int(255 * (x / max(1, fade - 1))))
    canvas.paste(region, (img_x0, 0), mask)

    # градиент читабельности: тёмный цвет ниши слева → прозрачный к ~55% ширины
    gw = int(W * 0.55)
    dr, dg, db = ImageColor.getrgb(dark)
    overlay = Image.new("RGBA", (gw, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    for x in range(gw):
        a = int(235 * (1 - x / max(1, gw - 1)) ** 1.25)
        od.line([(x, 0), (x, H)], fill=(dr, dg, db, a))
    canvas.paste(overlay, (0, 0), overlay)

    draw = ImageDraw.Draw(canvas)

    # текст: жёсткий кап 5 слов (3-секундное считывание), подбор кегля сверху вниз, ≤3 строк
    words = (text or "").strip().upper().split()
    text = " ".join(words[:5]) if words else "WATCH THIS"
    margin = 64
    max_w = int(W * 0.46)
    max_h = int(H * 0.70)
    chosen = None
    for size in range(168, 71, -8):
        font = ImageFont.truetype(font_file, size)
        stroke = max(6, size // 13)               # толстая обводка, масштабируется с кеглем
        lines = _wrap(draw, text, font, max_w, stroke)
        if len(lines) > 3:
            continue
        line_h = _line_height(draw, font, stroke)
        widest = max((_text_w(draw, ln, font, stroke) for ln in lines), default=0)
        if widest <= max_w and line_h * len(lines) <= max_h:
            chosen = (font, stroke, lines, line_h)
            break
    if chosen is None:                            # даже минимум не влез → держим кегль, режем строки
        font = ImageFont.truetype(font_file, 72)
        stroke = 6
        lines = _wrap(draw, text, font, max_w, stroke)[:3]
        chosen = (font, stroke, lines, _line_height(draw, font, stroke))
    font, stroke, lines, line_h = chosen

    block_h = line_h * len(lines)
    y = (H - block_h) // 2                        # вертикальный центр левой зоны
    for i, ln in enumerate(lines):
        fill = accent if (len(lines) > 1 and i == len(lines) - 1) else "#FFFFFF"
        draw.text((margin, y), ln, font=font, fill=fill,
                  stroke_width=stroke, stroke_fill="black")
        y += line_h
    if len(lines) == 1:                           # одна строка → акцент-подчёркивание
        bar_y = y + 12
        draw.rounded_rectangle([margin + stroke, bar_y, margin + stroke + int(max_w * 0.42),
                                bar_y + 14], radius=7, fill=accent)
        y = bar_y + 14

    _draw_accent_shape(draw, accent, W, H, img_x0, text_bottom=y)

    return _save_jpeg_capped(canvas, out_path, start_q=90)


# ──────────────────────────── Текст-утилиты (PIL 12) ────────────────────────────

def _text_w(draw, text: str, font, stroke: int) -> float:
    """Ширина строки с учётом обводки (textbbox точнее textlength на крупных кеглях)."""
    bb = draw.textbbox((0, 0), text, font=font, stroke_width=stroke)
    return bb[2] - bb[0]


def _line_height(draw, font, stroke: int) -> int:
    """Высота строки с обводкой + межстрочный воздух (~14%). Замер по латинским капсам
    (фабрика EN-only, текст обложки всегда ALL CAPS)."""
    bb = draw.textbbox((0, 0), "HQJY", font=font, stroke_width=stroke)
    return int((bb[3] - bb[1]) * 1.14)


def _wrap(draw, text: str, font, max_w: float, stroke: int) -> list[str]:
    """Перенос по словам так, чтобы каждая строка влезала в max_w (textbbox-замер)."""
    words = text.split()
    if not words:
        return [text]
    lines, cur = [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if _text_w(draw, trial, font, stroke) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


# ──────────────────────────── Из meta.json ────────────────────────────

# Служебные слова: обложка НЕ должна заканчиваться на них (иначе «…живут НА» — обрыв фразы).
_TAIL_STOP = frozenset({
    "на", "в", "во", "с", "со", "к", "ко", "по", "за", "из", "от", "до", "у", "о", "об", "про",
    "и", "а", "но", "или", "да", "же", "бы", "ли", "что", "как", "это", "не", "ни", "для", "без",
    "the", "a", "an", "of", "to", "in", "on", "at", "for", "and", "or", "but", "with", "your", "you",
})


def _strip_dangling(head: str) -> str:
    """Убрать висящие служебные слова в конце (предлог/союз/частица) — фраза не должна обрываться на них."""
    ws = head.split()
    while len(ws) > 1 and re.sub(r"[^\w]", "", ws[-1].lower()) in _TAIL_STOP:
        ws.pop()
    return " ".join(ws)


def _thumb_hook(meta: dict, fallback: str) -> str:
    """#9: короткий текст для обложки. Приоритет — meta['thumb_text'] (законченная фраза от LLM);
    иначе meta['hook']/hook_variant, режем до первой «фразы» и до N слов, БЕЗ висящих служебных слов."""
    tt = (meta.get("thumb_text", "") or "").strip()
    if tt:
        tt = re.split(r"[.!?\n]", tt, 1)[0].strip()      # одна фраза
        w = tt.split()
        if 1 <= len(w) <= 6 and len(tt) <= 30:           # валидный короткий thumb_text — берём как есть
            return _strip_dangling(tt) or tt
    raw = (meta.get("hook", "") or "").strip()
    if not raw:
        hv = meta.get("hook_variants") or []
        raw = (str(hv[0]).strip() if hv else "")
    if not raw:
        return fallback
    # обрезаем по первому знаку препинания (берём первую «фразу» хука)
    head = re.split(r"[.!?,:;—–\-…\n]", raw, 1)[0].strip()
    head = head or raw
    words = head.split()
    if len(words) > 5:
        head = " ".join(words[:5])
    if len(head) > 24:
        # режем по словам, чтобы не оборвать на полуслове и держаться ≤24 симв
        acc = []
        for w in head.split():
            cand = (" ".join(acc + [w])).strip()
            if len(cand) > 24:
                break
            acc.append(w)
        head = " ".join(acc) if acc else head[:24].rstrip()
    head = _strip_dangling(head)                          # финально — без висящего предлога/союза
    return head.strip() or fallback


def make_for_meta(video_path: str, meta: dict, out_path: pathlib.Path) -> pathlib.Path | None:
    """Удобная обёртка для пайплайна: заголовок и ниша берутся из meta.json ролика."""
    title = ""
    try:
        title = (meta.get("captions", {}).get("youtube", {}) or {}).get("title", "") or ""
    except (AttributeError, TypeError):
        title = ""
    if not title:
        title = meta.get("topic", "") or ""

    # #9: на обложку — короткий хук-текст, не длинный SEO-title (фолбэк на title)
    thumb_text = _thumb_hook(meta, title)

    niche = None
    niche_id = meta.get("niche")
    if niche_id:
        try:
            niche = core.get_niche(niche_id)
        except Exception as e:  # noqa: BLE001 — ниша не критична, обложка сделается на бренде
            core.log_error("thumbnail.make_for_meta(get_niche)", e, niche=niche_id)
            niche = None
    # #10: AI-фон по умолчанию вкл; THUMB_AI_BG=0 — вернуть прежнее поведение (кадр из видео)
    ai_bg = os.environ.get("THUMB_AI_BG", "1").strip().lower() not in ("0", "false", "no", "off", "")
    # формат: из ниши (format в niches.json) → core.ACTIVE_FORMAT на момент вызова
    fmt = (niche or {}).get("format") or core.ACTIVE_FORMAT
    return make_thumbnail(video_path, thumb_text, out_path, niche=niche, ai_bg=ai_bg, fmt=fmt)


def _thumb_texts(meta: dict, primary: str) -> list[str]:
    """Кандидаты текста обложки: приоритетный хук + альтернативы (варианты хука/заголовка).
    Уникальные, коротко-первыми (короткий крупный текст на обложке кликабельнее)."""
    cands = [primary]
    for t in (meta.get("hook_variants") or []):
        cands.append((t or "").strip())
    cands.append((meta.get("hook", "") or "").strip())
    for t in (meta.get("title_variants") or []):
        cands.append((t or "").strip())
    seen, out = set(), []
    for c in cands:
        c = _strip_dangling(c)
        k = c.lower()
        if c and k not in seen and 3 <= len(c) <= 60:
            seen.add(k); out.append(c)
    return out


def make_best_for_meta(video_path: str, meta: dict, out_path: pathlib.Path) -> pathlib.Path | None:
    """Авто-ВЫБОР обложки: генерим несколько вариантов (разный текст/сид), Gemini-зрение выбирает
    самый кликабельный и проверяет читаемость (Vision-QA обложки). Результат — в out_path;
    вердикт зрения кладём в meta['thumb_qa']. Fallback → обычная make_for_meta (один вариант).
    Гейт CF_THUMB_SELECT (по умолч. вкл); при отсутствии Gemini/PIL — тихий фолбэк."""
    if os.environ.get("CF_THUMB_SELECT", "1").strip().lower() in ("0", "false", "no", "off"):
        return make_for_meta(video_path, meta, out_path)
    try:
        from pipeline import vision
    except Exception:  # noqa: BLE001
        return make_for_meta(video_path, meta, out_path)
    if not vision.keys():
        return make_for_meta(video_path, meta, out_path)

    out_path = pathlib.Path(out_path)
    primary = _thumb_hook(meta, (meta.get("captions", {}).get("youtube", {}) or {}).get("title", "")
                          or meta.get("topic", ""))
    texts = _thumb_texts(meta, primary)
    n = max(2, min(3, int(os.environ.get("THUMB_SELECT_N", "2") or 2)))
    texts = texts[:n]
    if len(texts) < 2:                                   # нечего выбирать — обычный путь
        return make_for_meta(video_path, meta, out_path)

    niche = None
    if meta.get("niche"):
        try:
            niche = core.get_niche(meta["niche"])
        except Exception:  # noqa: BLE001
            niche = None
    ai_bg = os.environ.get("THUMB_AI_BG", "1").strip().lower() not in ("0", "false", "no", "off", "")
    fmt = (niche or {}).get("format") or core.ACTIVE_FORMAT

    variants = []
    for i, txt in enumerate(texts):
        cand = out_path.with_name(out_path.stem + f"_v{i}.jpg")
        seed = int(hashlib.md5((txt + str(i)).encode()).hexdigest()[:6], 16)
        res = make_thumbnail(video_path, txt, cand, niche=niche, ai_bg=ai_bg, seed=seed, fmt=fmt)
        if res:
            variants.append({"i": i, "text": txt, "path": pathlib.Path(res)})
    if not variants:
        return make_for_meta(video_path, meta, out_path)
    if len(variants) == 1:                               # сгенерился только один — берём его
        chosen = variants[0]["path"]
        chosen.replace(out_path)
        return out_path

    kind = ("vertical Shorts video" if fmt == "short"
            else "16:9 long-form YouTube documentary")
    prompt = (
        f"These are thumbnail candidates for a {kind} aimed at a US audience. "
        "Pick the MOST clickable one: large readable text, emotion/curiosity, strong contrast, "
        "one clear focal subject, no visual defects (warped faces/hands, garbled letters). "
        'Return STRICT JSON: {"best": <0-based index of the best>, "readable": true|false, '
        '"click_score": 1-10, "issue": "brief note on the best variant\'s problem, or empty"}.')
    verdict = vision.ask_json(prompt, [v["path"] for v in variants], max_tokens=300)

    best_i = 0
    if isinstance(verdict, dict) and isinstance(verdict.get("best"), int) \
            and 0 <= verdict["best"] < len(variants):
        best_i = verdict["best"]
    chosen = variants[best_i]["path"]
    # чистим проигравшие
    for v in variants:
        if v["path"] != chosen:
            v["path"].unlink(missing_ok=True)
    chosen.replace(out_path)

    meta["thumb_qa"] = {
        "chosen_text": variants[best_i]["text"],
        "variants": len(variants),
        "click_score": (verdict or {}).get("click_score"),
        "readable": (verdict or {}).get("readable"),
        "issue": (verdict or {}).get("issue", ""),
    }
    core.log(f"обложка: выбран вариант {best_i}/{len(variants)} "
             f"(score={(verdict or {}).get('click_score')}) «{variants[best_i]['text'][:40]}»",
             level="info")
    return out_path


if __name__ == "__main__":
    core.load_local_secrets()
    if len(sys.argv) >= 3:
        _video = sys.argv[1]
        _title = sys.argv[2]
        _out = pathlib.Path(_video).with_name(pathlib.Path(_video).stem + "_thumb.jpg")
        _res = make_thumbnail(_video, _title, _out)
        print(_res if _res else "FAILED")
    else:
        print("Использование: python3 pipeline/thumbnail.py <video.mp4> <заголовок>")
