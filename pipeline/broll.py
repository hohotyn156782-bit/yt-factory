"""Подбор B-roll под каждый кусок.

Каскад: Pexels (если есть PEXELS_API_KEY) → Pixabay (если PIXABAY_API_KEY) → генеративный
градиентный фон (ffmpeg, без ключей). Так первое видео собирается даже без единого ключа,
а с ключом подтягивается живое стоковое видео. Оба стока разрешают коммерческое использование
без обязательной атрибуции (см. README).

Возвращает список параллельно кускам: {"path", "kind": "stock"|"gen", "dur"}.
"""
import re
import json
import time
import hashlib
import pathlib
import urllib.parse
import urllib.request
import urllib.error

import sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import core  # noqa: E402

UA = "Mozilla/5.0 (content-factory)"


# ──────────────────────── Семантический ре-ранкинг стока под смысл сцены ────────────────────────
# Раньше брали files[0] (первый портретный) — случайный сток. Теперь собираем кандидатов и
# выбираем максимально близкий к запросу сцены по совпадению слов (slug у Pexels, tags у Pixabay).
# Бесплатно, без LLM-квоты, англ↔англ (поисковые запросы стока — на английском).

def _qtok(q: str) -> set:
    return {t for t in re.findall(r"\w+", (q or "").lower()) if len(t) >= 4}


def _overlap(qtoks: set, text: str) -> int:
    ttoks = {t for t in re.findall(r"\w+", (text or "").lower()) if len(t) >= 4}
    return len(qtoks & ttoks)


def _http_json(url: str, headers: dict | None = None, timeout: int = 30) -> dict | None:
    return core.http_json(url, headers=headers, timeout=timeout)   # с повторами + логом


def _download(url: str, dest: pathlib.Path, timeout: int = 90) -> bool:
    if dest.exists() and dest.stat().st_size > 10_000:
        return True
    return core.http_download(url, dest, timeout=timeout, min_bytes=10_000)   # с повторами + логом


# ──────────────────────────── Pexels ────────────────────────────

# Кэш ответов поиска в рамках одного запуска (ТОЛЬКО landscape/long): в длинном ролике запросы
# семейства главы повторяются на многих слотах → 1 HTTP-запрос на уникальный запрос, из ответа
# берём несколько файлов (дедуп через used). Экономит лимит Pexels 200 req/h.
_SEARCH_CACHE: dict[str, dict] = {}


def _landscape() -> bool:
    """Горизонтальный ли активный формат — ЧИТАЕМ core.W/H в момент вызова (set_format)."""
    return core.W >= core.H


def _pexels(query: str, used: set, cache: pathlib.Path) -> tuple[str, str] | None:
    key = core.secret("PEXELS_API_KEY", required=False)
    if not key:
        return None
    land = _landscape()
    url = "https://api.pexels.com/videos/search?" + urllib.parse.urlencode({
        "query": query, "orientation": "landscape" if land else "portrait",
        "size": "medium", "per_page": 25 if land else 15,
    })
    if land and url in _SEARCH_CACHE:
        data = _SEARCH_CACHE[url]
    else:
        data = _http_json(url, headers={"Authorization": key})
        if land and data:
            _SEARCH_CACHE[url] = data
    if not data:
        return None
    qt = _qtok(query)
    cands = []   # (релевантность, vid, link) — ранжируем по совпадению со сценой
    for vid in data.get("videos", []):
        if f"pexels:{vid['id']}" in used:
            continue
        if land:
            files = [f for f in vid.get("video_files", []) if f.get("file_type") == "video/mp4"
                     and (f.get("width") or 0) >= (f.get("height") or 0)]  # ландшафт
            if not files:
                continue
            # ближе всего к 1920 по ширине, но не гигант (4K тянуть незачем)
            files.sort(key=lambda f: abs((f.get("width") or 0) - 1920) + (1 if (f.get("width") or 0) > 2600 else 0))
        else:
            files = [f for f in vid.get("video_files", []) if f.get("file_type") == "video/mp4"
                     and (f.get("height") or 0) >= (f.get("width") or 0)]  # портрет
            if not files:
                continue
            # ближе всего к 1080x1920, но не гигант
            files.sort(key=lambda f: abs((f.get("height") or 0) - 1920) + (1 if (f.get("height") or 0) > 2200 else 0))
        link = files[0].get("link")
        if not link:
            continue
        # релевантность: слова из slug страницы (Pexels не даёт тегов/alt у видео)
        slug = (vid.get("url", "") or "").replace("-", " ").replace("/", " ")
        cands.append((_overlap(qt, slug), vid, link))
    cands.sort(key=lambda c: -c[0])   # сначала самые релевантные сцене
    for _score, vid, link in cands:
        dest = cache / f"pexels_{vid['id']}.mp4"
        if _download(link, dest):
            used.add(f"pexels:{vid['id']}")
            return str(dest), vid.get("url", "")
    return None


# ──────────────────────────── Pixabay ────────────────────────────

def _pixabay(query: str, used: set, cache: pathlib.Path) -> tuple[str, str] | None:
    key = core.secret("PIXABAY_API_KEY", required=False)
    if not key:
        return None
    url = "https://pixabay.com/api/videos/?" + urllib.parse.urlencode({
        "key": key, "q": query, "per_page": 20, "safesearch": "true",
    })
    data = _http_json(url)
    if not data:
        return None
    qt = _qtok(query)
    cands = []   # (релевантность по тегам, hit, link)
    for hit in data.get("hits", []):
        hid = hit.get("id")
        if f"pixabay:{hid}" in used:
            continue
        renders = hit.get("videos", {})
        if _landscape():
            # ландшафт: только рендеры width>=height, ближайший к 1920 по ширине
            rl = [r for r in renders.values()
                  if r.get("url") and (r.get("width") or 0) >= (r.get("height") or 0)]
            rl.sort(key=lambda r: abs((r.get("width") or 0) - 1920))
            pick = rl[0] if rl else None
        else:
            pick = renders.get("large") or renders.get("medium") or renders.get("small")
        if not pick or not pick.get("url"):
            continue
        cands.append((_overlap(qt, hit.get("tags", "")), hit, pick["url"]))   # у Pixabay есть теги
    cands.sort(key=lambda c: -c[0])
    for _score, hit, link in cands:
        hid = hit.get("id")
        dest = cache / f"pixabay_{hid}.mp4"
        if _download(link, dest):
            used.add(f"pixabay:{hid}")
            return str(dest), hit.get("pageURL", "")
    return None


# ──────────────────────────── Coverr (ресёрч р2: чистый API, без карты) ────────────────────────────

def _coverr(query: str, used: set, cache: pathlib.Path) -> tuple[str, str] | None:
    """Coverr API — 3-й источник стока. Бесплатно для коммерции/монетизации без атрибуции.
    Ключ COVERR_API_KEY (free, без карты, coverr.co/developers). Молча пропускаем без ключа."""
    key = core.secret("COVERR_API_KEY", required=False)
    if not key:
        return None
    url = "https://api.coverr.co/videos?" + urllib.parse.urlencode({
        "query": query, "page_size": 20, "urls": "true",
    })
    data = _http_json(url, headers={"Authorization": f"Bearer {key}"})
    if not data:
        return None
    for hit in data.get("hits", data.get("videos", [])):
        hid = hit.get("id") or hit.get("_id")
        if not hid or f"coverr:{hid}" in used:
            continue
        # ландшафт: если API отдал размеры и клип портретный — пропускаем (dims есть не всегда)
        if _landscape():
            cw = hit.get("max_width") or hit.get("width") or 0
            chh = hit.get("max_height") or hit.get("height") or 0
            if cw and chh and cw < chh:
                continue
        urls = hit.get("urls", {}) or {}
        link = urls.get("mp4_download") or urls.get("mp4") or hit.get("download_url")
        if not link:
            continue
        dest = cache / f"coverr_{str(hid)[:16]}.mp4"
        if _download(link, dest):
            used.add(f"coverr:{hid}")
            return str(dest), f"https://coverr.co/videos/{hid}"
    return None


# ──────────────────────────── NASA SVS (public domain, без ключа) ────────────────────────────

# NASA Scientific Visualization Studio: видео в public domain, без ключа. Только science/space/tech.
NASA_NICHES = {"ai", "tech", "science", "space", "climate", "education", "data", "future"}


def _nasa_compatible(niche: dict, query: str) -> bool:
    """NASA звать ТОЛЬКО для совместимых тем: пересечение по niche id или ключевым словам запроса."""
    nid = (niche.get("id", "") or "").lower()
    if any(n in nid for n in NASA_NICHES):
        return True
    qtoks = {t for t in re.findall(r"\w+", (query or "").lower())}
    return bool(qtoks & NASA_NICHES)


def _nasa_svs(query: str, used: set, cache: pathlib.Path) -> tuple[str, str] | None:
    """NASA SVS, 2-шаговая схема (search → detail). PD-футаж, любые ошибки → None (фолбэк дальше)."""
    try:
        s_url = "https://svs.gsfc.nasa.gov/api/search/?" + urllib.parse.urlencode({
            "search": query, "limit": 10,
        })
        data = _http_json(s_url)
        if not data:
            return None
        for res in data.get("results", []):
            sid = res.get("id")
            if sid is None or f"nasa:{sid}" in used:
                continue
            time.sleep(1.0)                                  # не долбить detail-эндпоинт
            detail = _http_json(f"https://svs.gsfc.nasa.gov/api/{sid}/")
            if not detail:
                continue
            best = None  # (area, link)
            for grp in detail.get("media_groups", []):
                for it in grp.get("items", []):
                    inst = it.get("instance", {}) or {}
                    if inst.get("media_type") != "Movie":
                        continue
                    fn = inst.get("filename", "") or ""
                    if not fn.endswith(".mp4"):
                        continue
                    w = inst.get("width") or 0
                    h = inst.get("height") or 0
                    if _landscape() and w < h:      # ландшафт: только горизонтальные рендеры
                        continue
                    if min(w, h) < 1080:
                        continue
                    area = w * h
                    if area > 1920 * 1080 * 1.1:
                        continue
                    link = inst.get("url") or fn
                    if not link or not str(link).startswith("http"):
                        continue
                    if best is None or area > best[0]:
                        best = (area, link)
            if not best:
                continue
            dest = cache / f"nasa_{sid}.mp4"
            if core.http_download(best[1], dest, timeout=180, min_bytes=100000):
                used.add(f"nasa:{sid}")
                return str(dest), f"https://svs.gsfc.nasa.gov/{sid}/"
    except Exception:  # noqa: BLE001 — новый источник не должен ронять каскад
        return None
    return None


# ──────────────────────────── Internet Archive (PD / CC-BY, без ключа) ────────────────────────────

# Только чистый public domain ИЛИ CC BY. Любые by-nc/by-nd/by-sa/by-nc-* отбраковываем.
_LIC_BAD = ("by-nc", "by-nd", "by-sa", "nc-", "noderiv", "noncommercial", "sharealike")


def _archive_lic_ok(lic: str) -> bool:
    l = (lic or "").lower()
    if any(bad in l for bad in _LIC_BAD):
        return False
    if "publicdomain" in l or "/publicdomain/" in l:
        return True
    if "creativecommons.org/licenses/by/" in l or l.rstrip("/").endswith("/by"):
        return True
    return False


def _archive_org(query: str, used: set, cache: pathlib.Path) -> tuple[str, str] | None:
    """Internet Archive (advancedsearch + metadata). Только PD / CC BY. Ошибки → None."""
    try:
        q = (f'({query}) AND mediatype:movies AND '
             f'(licenseurl:*publicdomain* OR licenseurl:*creativecommons.org\\/licenses\\/by\\/*)')
        s_url = "https://archive.org/advancedsearch.php?" + urllib.parse.urlencode({
            "q": q, "output": "json", "rows": 20,
            "fl[]": ["identifier", "title", "licenseurl"],
        }, doseq=True)
        data = _http_json(s_url)
        if not data:
            return None
        docs = (data.get("response", {}) or {}).get("docs", [])
        for doc in docs:
            ident = doc.get("identifier")
            if not ident or f"archive:{ident}" in used:
                continue
            if not _archive_lic_ok(doc.get("licenseurl", "")):
                continue
            meta = _http_json(f"https://archive.org/metadata/{ident}")
            if not meta:
                continue
            # двойная страховка: лицензия из metadata тоже должна пройти
            mlic = (meta.get("metadata", {}) or {}).get("licenseurl", "")
            if mlic and not _archive_lic_ok(mlic):
                continue
            best = None  # (area_proxy_size, filename)
            for f in meta.get("files", []):
                fmt = (f.get("format", "") or "").lower()
                if "h.264" not in fmt and "mpeg4" not in fmt and "mpeg-4" not in fmt:
                    continue
                fn = f.get("name", "")
                if not fn:
                    continue
                try:
                    size = int(f.get("size", 0))
                except (TypeError, ValueError):
                    size = 0
                if not (500 * 1024 < size < 150 * 1024 * 1024):
                    continue
                # наибольшее разрешение приближаем через width*height, иначе по размеру файла
                try:
                    w = int(f.get("width", 0)); h = int(f.get("height", 0))
                except (TypeError, ValueError):
                    w = h = 0
                if _landscape() and w and h and w < h:   # ландшафт: портретные файлы мимо
                    continue
                rank = (w * h) if (w and h) else size
                if best is None or rank > best[0]:
                    best = (rank, fn)
            if not best:
                continue
            fn = best[1]
            link = f"https://archive.org/download/{ident}/{urllib.parse.quote(fn)}"
            dest = cache / f"archive_{re.sub(r'[^A-Za-z0-9_.-]', '_', ident)[:40]}.mp4"
            if core.http_download(link, dest, timeout=120, min_bytes=100000):
                used.add(f"archive:{ident}")
                return str(dest), f"https://archive.org/details/{ident}"
    except Exception:  # noqa: BLE001 — новый источник не должен ронять каскад
        return None
    return None


# ──────────────────────────── Генеративный фолбэк ────────────────────────────

def _gen_gradient(palette: list, idx: int, dur: float, out: pathlib.Path) -> str:
    pair = palette[idx % len(palette)] if palette else ["0a0a14", "2aa6ff"]
    a, b = pair[0].lstrip("#"), pair[1].lstrip("#")
    src = (f"gradients=s={core.W}x{core.H}:c0=0x{a}:c1=0x{b}"
           f":nb_colors=2:seed={idx * 7 + 3}:speed=0.011:duration={max(dur, 1.0):.2f}")
    # без temporal-noise: зерно несжимаемо и раздувает файл в разы; гладкий градиент весит копейки
    vf = "vignette=PI/5,format=yuv420p"
    core.run([
        "ffmpeg", "-y", "-f", "lavfi", "-i", src, "-t", f"{max(dur, 1.0):.2f}",
        "-vf", vf, "-r", str(core.FPS), "-an", str(out),
    ])
    return str(out)


# ──────────────────────────── Оркестрация ────────────────────────────

# вариативная длина плана (ресёрч: 2-4с, НЕ ровно — ровный ритм = AI-слоп)
SLOT_CYCLE = [3.2, 4.1, 2.8, 3.6, 3.0, 4.4, 2.9, 3.8]
INTRO_SLOTS = [1.0, 1.0, 1.0]   # pattern-interrupt: ≥3 смены кадра в первые ~3с (бьёт в главный дроп-офф 0-3с)


SNAP_WINDOW = 0.6   # окно примагничивания конца слота к ближайшей границе chunk'а (±с)

# ── long-form (16:9 документалистика): свой темп монтажа ──
# Слоты 6-14с (вариативно, ровный ритм = слоп), первые ~15с ПЛОТНЕЕ (3-5с — интро-удержание).
# На 10-12 мин выходит ~60-78 ассетов (в целевой вилке 55-90).
LONG_SLOT_CYCLE = [8.0, 11.5, 6.5, 13.0, 7.0, 9.5, 12.5, 6.0, 10.5, 14.0]
LONG_INTRO_SLOTS = [3.5, 4.0, 3.0, 4.5]   # = 15с плотного интро
LONG_SNAP_WINDOW = 1.8   # фразы длиннее → окно примагничивания шире


def _slots(total: float, boundaries: list[float] | None = None) -> list[float]:
    """Разбить таймлайн на план-слоты. Первые ~3с — ПЛОТНОЕ интро (3 быстрых смены),
    дальше вариативная длина (≈3-4с). Сумма = total.

    boundaries (#7): отсортированные концы chunk'ов сценария (timed_chunks['end']).
    Для НЕ-интро слотов конец слота тянется до ближайшей такой границы в окне ±SNAP_WINDOW
    от целевой SLOT_CYCLE-длины → смена кадра совпадает с концом фразы (осознанный кат).
    Нет границ/попаданий в окно → fallback на ровную SLOT_CYCLE-длину. Сумма СОХРАНЯЕТСЯ
    (последний слот добирает остаток); интро-логика не затрагивается."""
    bnds = sorted(boundaries) if boundaries else []
    out, acc = [], 0.0
    for d in INTRO_SLOTS:                         # плотное интро (удержание в критичные 0-3с) — НЕ магнитим
        if acc >= total - 0.4:
            break
        d = min(d, total - acc)
        out.append(round(d, 2)); acc += d
    i = 0
    while acc < total - 0.4:                      # остальное — вариативный ритм
        d = SLOT_CYCLE[i % len(SLOT_CYCLE)]
        target_end = acc + d
        if bnds:                                  # #7: примагнитить конец слота к концу фразы
            snapped = _snap_end(target_end, acc, bnds)
            if snapped is not None:
                d = snapped - acc
        if acc + d > total:
            d = total - acc
        out.append(round(d, 2)); acc += d; i += 1
    if not out:
        out = [max(total, 1.0)]
    return out


def _snap_end(target_end: float, slot_start: float, bnds: list[float],
              window: float = SNAP_WINDOW) -> float | None:
    """Ближайшая граница chunk'а к target_end в окне ±window, строго после slot_start.
    None → подходящей границы нет (fallback на SLOT_CYCLE-длину)."""
    best = None
    for b in bnds:
        if b <= slot_start + 0.05:               # слот должен иметь положительную длину
            continue
        if abs(b - target_end) <= window and (best is None or abs(b - target_end) < abs(best - target_end)):
            best = b
    return best


def _slots_long(total: float, boundaries: list[float] | None = None) -> list[float]:
    """План-слоты long-form: первые ~15с плотные (3-5с, интро-удержание), дальше 6-14с
    с примагничиванием концов к границам фраз (окно ±LONG_SNAP_WINDOW). Сумма = total."""
    bnds = sorted(boundaries) if boundaries else []
    out, acc = [], 0.0
    for d in LONG_INTRO_SLOTS:                    # плотное интро первые ~15с
        if acc >= total - 0.4:
            break
        target_end = acc + d
        if bnds:                                  # интро тоже режем по фразам (узкое окно)
            snapped = _snap_end(target_end, acc, bnds, window=SNAP_WINDOW)
            if snapped is not None:
                d = snapped - acc
        d = min(d, total - acc)
        out.append(round(d, 2)); acc += d
    i = 0
    while acc < total - 0.4:                      # тело: вариативные 6-14с по фразам
        d = LONG_SLOT_CYCLE[i % len(LONG_SLOT_CYCLE)]
        target_end = acc + d
        if bnds:
            snapped = _snap_end(target_end, acc, bnds, window=LONG_SNAP_WINDOW)
            if snapped is not None:
                d = snapped - acc
        if acc + d > total:
            d = total - acc
        out.append(round(d, 2)); acc += d; i += 1
    if not out:
        out = [max(total, 1.0)]
    return out


def _query_at(timed_chunks: list[dict], t: float, fallback: str) -> str:
    """Запрос b-roll для момента t: берём из сегмента сценария, в который попадает t."""
    for ch in timed_chunks:
        if ch["start"] <= t < ch["end"]:
            return (ch.get("broll_query") or fallback).strip()
    return (timed_chunks[-1].get("broll_query") if timed_chunks else fallback) or fallback


def _long_families(timed_chunks: list[dict]) -> list[dict]:
    """Семейства запросов long-form: подряд идущие chunk'и ОДНОЙ главы (поле 'chapter')
    сливаются в одно семейство {start, end, queries[]}. Нет поля chapter → каждый chunk
    отдельное семейство (поведение = запрос своего сегмента, как у _query_at)."""
    fams: list[dict] = []
    for ch in timed_chunks:
        q = (ch.get("broll_query") or "").strip()
        st = float(ch.get("start", 0.0) or 0.0)
        en = float(ch.get("end", st) or st)
        key = ch.get("chapter")
        if fams and key is not None and fams[-1]["key"] == key:
            fams[-1]["end"] = max(fams[-1]["end"], en)
            if q and q not in fams[-1]["queries"]:
                fams[-1]["queries"].append(q)
        else:
            fams.append({"key": key, "start": st, "end": en, "queries": [q] if q else []})
    return fams


def _long_query(fams: list[dict], t: float, rot: dict, fallback: str) -> str:
    """Запрос для слота long-form в момент t: ротация по запросам семейства главы.
    Повтор запроса внутри главы намеренный — поисковый кэш Pexels отдаёт СЛЕДУЮЩИЙ файл
    из того же ответа (батчинг под лимит 200 req/h). rot — счётчики ротации по семействам."""
    for fi, f in enumerate(fams):
        if f["start"] <= t < f["end"] or (fi == len(fams) - 1 and t >= f["end"]):
            qs = f["queries"]
            if not qs:
                return fallback
            k = rot.get(fi, 0)
            rot[fi] = k + 1
            return qs[k % len(qs)]
    return fallback


# pattern-interrupt в первые ~3с: 3 интро-слота не должны брать ОДИН хуковый чанк
# → 3 похожих кадра. Варьируем поисковый запрос модификаторами ПЛАНА (не темы),
# плюс по возможности тянем базу из РАЗНЫХ чанков сценария — кадры выходят контрастные,
# но релевантность темы сохраняется.
INTRO_VARY = ["close up", "wide establishing shot", "fast motion", "extreme close-up",
              "overhead top-down shot", "low angle dramatic", "slow motion", "side profile angle",
              "dynamic tracking shot", "macro detail"]
# вариации композиции для внутри-видео дедупа AI-кадров (повтор запроса → другой ракурс/свет)
DEDUP_VARY = ["different angle", "different lighting", "another perspective", "wider framing",
              "closer view", "different setting", "alternate composition", "different time of day"]

# эпохо-специфичный запрос: такого стока не существует физически → слот уходит в FLUX-реконструкцию
_ERA_RE = re.compile(
    r"\b(1[0-8]\d\d|19[0-4]\d|ancient|medieval|victorian|roman|colonial|renaissance|byzantine|"
    r"ottoman|pharaoh|viking|samurai|dynasty|(?:1st|2nd|3rd|[4-9]th|1[0-9]th)[ -]century)s?\b", re.I)


def _intro_query(timed_chunks: list[dict], t: float, idx: int, base: str) -> str:
    """Вариативный запрос для интро-слота idx (0..len(INTRO_SLOTS)-1).
    База: broll_query разных чанков (по индексу слота, если их хватает), иначе чанк момента t.
    К базе добавляется модификатор плана INTRO_VARY[idx] для контраста кадров.
    Чистый помощник: при пустой базе откатывается на base."""
    chunks_q = [(c.get("broll_query") or "").strip() for c in timed_chunks]
    chunks_q = [q for q in chunks_q if q]
    if len(chunks_q) > idx:
        bq = chunks_q[idx]                         # (б) разные чанки → разный визуал
    else:
        bq = _query_at(timed_chunks, t, base)      # чанков мало → чанк момента t
    bq = (bq or base).strip()
    suffix = INTRO_VARY[idx % len(INTRO_VARY)]     # (а) модификатор плана
    return f"{bq} {suffix}".strip() if bq else base


def _img_seed(workdir: pathlib.Path, i: int) -> int:
    """Seed AI-картинки, уникальный по (видео, слот). workdir.name кодирует нишу+тему+таймстамп →
    один и тот же слот в РАЗНЫХ роликах/нишах даёт РАЗНЫЙ FLUX-шум (анти-визуальная-кластеризация)."""
    base = int(hashlib.md5(str(workdir.name).encode()).hexdigest()[:7], 16)
    return (base + i * 7 + 13) & 0x7fffffff


def _ai_image_slot(query: str, niche: dict, workdir: pathlib.Path, i: int, character: str = "") -> dict | None:
    from pipeline import imagegen
    img = imagegen.generate(query, niche, workdir / f"img_{i:02d}.png", seed=_img_seed(workdir, i), character=character)
    if img:
        return {"path": img, "kind": "image", "source": "ai_image", "source_url": "", "query": query}
    return None


import os as _os
import subprocess as _sp
_DEPTH_PY = pathlib.Path(__file__).resolve().parent.parent / ".venv-depth" / "bin" / "python"


def _depthflow_clip(image_path: str, dur: float, workdir: pathlib.Path, i: int) -> str | None:
    """Картинка → параллакс-видео (3D-движение камеры) через DepthFlow, локально на CPU. None при сбое."""
    if not _DEPTH_PY.exists():
        return None
    out = workdir / f"depth_{i:02d}.mp4"
    env = {**_os.environ, "LIBGL_ALWAYS_SOFTWARE": "1"}
    cmd = [str(_DEPTH_PY), "-m", "depthflow", "input", "-i", image_path,
           "main", "--width", str(core.W), "--height", str(core.H),
           "--time", f"{dur:.2f}", "--fps", str(core.FPS), "-o", str(out)]
    try:
        _sp.run(cmd, env=env, capture_output=True, timeout=300)
        return str(out) if out.exists() and out.stat().st_size > 10_000 else None
    except Exception:  # noqa: BLE001
        return None


def _ai_depth_slot(query: str, niche: dict, workdir: pathlib.Path, i: int, dur: float) -> dict | None:
    """AI-картинка → DepthFlow параллакс-видео. Фолбэк на саму картинку (Ken Burns), если depth не вышел."""
    from pipeline import imagegen
    img = imagegen.generate(query, niche, workdir / f"img_{i:02d}.png", seed=_img_seed(workdir, i))
    if not img:
        return None
    vid = _depthflow_clip(img, dur, workdir, i)
    if vid:
        return {"path": vid, "kind": "video", "source": "depthflow", "source_url": "", "query": query}
    return {"path": img, "kind": "image", "source": "ai_image", "source_url": "", "query": query}


def _pollinations_video(query: str, niche: dict, workdir: pathlib.Path, i: int) -> dict | None:
    """Бесплатное AI-видео (wan-fast, 480p) через Pollinations — для хук-клипа варианта B."""
    from pipeline import imagegen
    prompt = imagegen.prompt_for(query, niche).replace("photograph", "video, slow camera motion")
    q = urllib.parse.quote(prompt)
    out = workdir / f"vid_{i:02d}.mp4"
    vw, vh = (1080, 608) if _landscape() else (608, 1080)   # формат-зависимые размеры
    for model in ("wan-fast", "wan"):
        url = (f"https://gen.pollinations.ai/video/{q}?model={model}"
               f"&width={vw}&height={vh}&duration=5&nologo=true")
        if _download(url, out, timeout=180):
            return {"path": str(out), "kind": "video", "source": f"pollinations_{model}",
                    "source_url": "", "query": query}
    return None


def fetch_for(timed_chunks: list[dict], niche: dict, workdir: pathlib.Path,
              mode: str = "stock", character: str = "") -> list[dict]:
    """mode: 'stock' (Pexels) | 'ai_images' (картинки+KenBurns) | 'ai_video_hook' (картинки + видео на хук)
    | 'mixed' (long: сток + доля FLUX-картинок с Ken Burns для сцен, которых нет в стоке —
    историческая реконструкция; доля из niche['broll_ai_ratio'], дефолт 0.35).
    character — для story: единый персонаж во всех ИИ-кадрах (визуальная связность)."""
    core.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    workdir.mkdir(parents=True, exist_ok=True)
    palette = niche.get("palette", [])
    fallback_q = niche.get("broll_hint", "abstract").split(",")[0].strip()
    total = sum(c["dur"] for c in timed_chunks) or 1.0
    # анти-дубль видеоряда МЕЖДУ роликами: подтягиваем уже использованные в нише клипы (#3 аудита)
    niche_id = niche.get("id", "")
    try:
        from pipeline import topics_db
        used: set = set(topics_db.recent_media(niche_id, days=21)) if niche_id else set()
    except Exception:  # noqa: BLE001
        used = set()
    seeded = set(used)
    downloaded: list[tuple[str, str]] = []
    out: list[dict] = []
    seen_q: set = set()                                  # #кач: нормализованные запросы внутри ЭТОГО ролика

    # #7: границы для примагничивания концов слотов = концы chunk'ов сценария.
    # fallback-safe: нет поля 'end' / пусто → boundaries=None → _slots ведёт себя как раньше (SLOT_CYCLE).
    boundaries = [float(c["end"]) for c in timed_chunks if c.get("end") is not None]
    if boundaries:
        boundaries = sorted(b for b in boundaries if 0.0 < b < total)   # внутренние границы (хвост = total добирается сам)

    # long-form: свой планировщик слотов (6-14с + плотное интро) и запросы семейств глав
    is_long = core.ACTIVE_FORMAT == "long"
    _SEARCH_CACHE.clear()                                # кэш поиска живёт в рамках одного ролика
    fams = _long_families(timed_chunks) if is_long else None
    fam_rot: dict = {}                                   # счётчики ротации запросов по семействам
    slot_plan = _slots_long(total, boundaries or None) if is_long else _slots(total, boundaries or None)
    # mixed: доля AI-слотов из ниши; интро-слоты всегда сток (живое движение важнее в первые 15с)
    ai_ratio = float(niche.get("broll_ai_ratio", 0.35) or 0.0) if mode == "mixed" else 0.0
    ai_done = 0

    cursor = 0.0
    for i, dur in enumerate(slot_plan):
        query = _query_at(timed_chunks, cursor + dur / 2, fallback_q)
        if is_long:
            # long: запрос из семейства главы (ротация внутри семейства, батчинг Pexels-кэшем)
            search_query = _long_query(fams, cursor + dur / 2, fam_rot, query)
        elif i < len(INTRO_SLOTS):
            # интро-слоты (первые ~3с): вариативный запрос → контрастные кадры (pattern-interrupt).
            # search_query идёт в источники; query остаётся базой для метаданных и fallback-отката.
            search_query = _intro_query(timed_chunks, cursor + dur / 2, i, query)
            if search_query != query:
                core.log("broll intro vary", level="debug", slot=i, base=query, search=search_query)
        else:
            search_query = query
        # #кач: пустой/слабый запрос (нет значимых токенов) → тематический fallback,
        # иначе сток вернёт случайный нерелевантный кадр (находка аудита).
        if not _qtok(search_query):
            search_query = fallback_q or search_query
        # mixed: этот слот — AI-картинка? Равномерное распределение доли ai_ratio по таймлайну,
        # интро (первые LONG_INTRO_SLOTS) не трогаем. Эпохо-специфичный запрос (год/эра) идёт
        # в AI ВНЕ квоты ratio: стока 1860-х не существует, Pexels отдаёт современный город
        # в джинсах — анахронизм хуже, чем лишний FLUX-кадр (дневной лимит NVIDIA всё равно
        # гейтит в _ai_image_slot: при исчерпании item=None → сток-каскад).
        era_q = bool(_ERA_RE.search(search_query))
        ai_slot = (mode == "mixed" and i >= len(LONG_INTRO_SLOTS)
                   and (era_q or ai_done < ai_ratio * (i + 1 - len(LONG_INTRO_SLOTS))))
        # #кач: внутри-видео дедуп AI-кадров — повтор того же запроса → добавляем вариацию композиции,
        # чтобы FLUX дал ДРУГОЙ кадр (seed уже разный, но одинаковый промпт даёт похожую сцену).
        if mode in ("ai_images", "ai_video_hook", "depth_video") or ai_slot:
            qnorm = " ".join(sorted(_qtok(search_query)))
            if qnorm and qnorm in seen_q:
                search_query = f"{search_query} {DEDUP_VARY[i % len(DEDUP_VARY)]}"
                core.log("broll dedup vary", level="debug", slot=i, q=search_query)
            seen_q.add(qnorm)
        item = None

        if ai_slot:                                       # mixed: FLUX-реконструкция + Ken Burns (в assemble)
            item = _ai_image_slot(search_query, niche, workdir, i, character=character)
            if item:
                ai_done += 1                              # квота исчерпана/сбой → item=None → сток-каскад ниже

        if mode == "depth_video":                            # AI-картинка → DepthFlow параллакс-видео
            item = _ai_depth_slot(search_query, niche, workdir, i, dur)

        if mode in ("ai_images", "ai_video_hook"):
            # вариант B: на хук — РЕАЛЬНОЕ стоковое видео (живое движение там, где важнее всего),
            # т.к. бесплатного AI-видео нет (Pollinations video = 401/платно). Остальное — AI-картинки.
            if mode == "ai_video_hook" and i == 0:
                res = _pexels(search_query, used, core.CACHE_DIR) or _pexels(fallback_q, used, core.CACHE_DIR)
                if res:
                    path, src_url = res
                    item = {"path": path, "kind": "stock", "source": "pexels",
                            "source_url": src_url, "query": search_query}
            if item is None:                                  # основная масса — AI-картинка
                item = _ai_image_slot(search_query, niche, workdir, i, character=character)

        if item is None:                                      # фолбэк: сток (Pexels→Pixabay→Coverr→NASA→Archive)
            res = (_pexels(search_query, used, core.CACHE_DIR) or _pixabay(search_query, used, core.CACHE_DIR)
                   or _coverr(search_query, used, core.CACHE_DIR) or _pexels(fallback_q, used, core.CACHE_DIR))
            # fallback-safe: вариативный интро-запрос не дал кадра → откат на базовый _query_at-запрос
            # (не теряем релевантность, не ослабляем каскад) перед reuse/gradient.
            if res is None and search_query != query:
                res = (_pexels(query, used, core.CACHE_DIR) or _pixabay(query, used, core.CACHE_DIR)
                       or _coverr(query, used, core.CACHE_DIR))
            # NASA SVS — ТОЛЬКО для совместимых тем (наука/космос/тех), затем Internet Archive (любая тема).
            # Оба бесплатны и без ключа; стоят ПОСЛЕ Coverr и ПЕРЕД генеративным фолбэком.
            if res is None and _nasa_compatible(niche, query):
                res = _nasa_svs(query, used, core.CACHE_DIR)
            if res is None:
                res = _archive_org(query, used, core.CACHE_DIR)
            if res:
                path, src_url = res
                downloaded.append((path, src_url))
                if "pexels" in path:
                    src = "pexels"
                elif "pixabay" in path:
                    src = "pixabay"
                elif "nasa_" in path:
                    src = "nasa_svs"
                elif "archive_" in path:
                    src = "archive_org"
                else:
                    src = "coverr"
                item = {"path": path, "kind": "stock", "source": src,
                        "source_url": src_url, "query": query}
            elif downloaded:
                path, src_url = downloaded[i % len(downloaded)]
                item = {"path": path, "kind": "stock", "source": "reuse",
                        "source_url": src_url, "query": query}
            else:
                item = {"path": _gen_gradient(palette, i, dur, workdir / f"bg_{i:02d}.mp4"),
                        "kind": "gen", "source": "generated", "source_url": "", "query": query}

        item["dur"] = dur
        out.append(item)
        cursor += dur
    # запоминаем НОВЫЕ использованные клипы в нише (для дедупа следующих роликов)
    new_keys = used - seeded
    if niche_id and new_keys:
        try:
            from pipeline import topics_db
            topics_db.record_media(niche_id, new_keys)
        except Exception:  # noqa: BLE001
            pass
    # видимость качества видеоряда: доля слабых кадров (генеративный градиент/реюз). warn при перекосе
    # (>20% слотов) — сигнал, что запросы/источники по нише сбоят; видео при этом НЕ блокируется.
    try:
        from collections import Counter
        mix = Counter(it.get("source", "?") for it in out)
        weak = mix.get("generated", 0) + mix.get("reuse", 0)
        if out:
            core.log(f"broll источники: {dict(mix)}",
                     level="warn" if weak > max(1, len(out) // 5) else "info",
                     slots=len(out), weak=weak, niche=niche_id)
    except Exception:  # noqa: BLE001
        pass
    return out


if __name__ == "__main__":
    core.load_local_secrets()
    chunks = [{"dur": 3.0, "broll_query": "artificial intelligence"},
              {"dur": 4.0, "broll_query": "data center"}]
    res = fetch_for(chunks, core.get_niche("ai_lifehacks"), core.OUTPUT_DIR / "_broll_test")
    for r in res:
        print(r["kind"], r["dur"], r["path"])
