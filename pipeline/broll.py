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
import os
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


def _archive_org(query: str, used: set, cache: pathlib.Path, extra: str = "") -> tuple[str, str] | None:
    """Internet Archive (advancedsearch + metadata). Только PD / CC BY. Ошибки → None.
    extra — дополнительное условие поиска (archival: без зеркал YouTube)."""
    try:
        q = (f'({query}) AND mediatype:movies AND '
             f'(licenseurl:*publicdomain* OR licenseurl:*creativecommons.org\\/licenses\\/by\\/*)' + extra)
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


# ──────────────── Архивные кадры (broll_mode "archival": настоящие фото/хроника/карты) ────────────────
# Только лицензии, разрешающие коммерческое использование: PD / CC0 / CC BY / CC BY-SA
# (+ «no known restrictions» Библиотеки Конгресса). NC/ND, GFDL-only, «Attribution»-кастом,
# всё без явной свободной лицензии — мимо. Агентства (Getty/AP/Reuters/Pathé/BBC) — мимо даже с PD-меткой.
# Каждый кадр проверяет Gemini vision (imagegen.archival_check): релевантность сцене, graphic
# (трупы/кровь/раненые → возрастные ограничения), водяные знаки стоков. Нет кадра → карта кодом.

_ARCH_UA = {"User-Agent": "yt-factory-archival/1.0 (educational history videos; python-urllib)"}
_ARCH_BAD_SRC = re.compile(r"getty|associated press|\bap photo|reuters|path[eé]|\bbbc\b|corbis|alamy|"
                           r"shutterstock|afp\b|bettmann|magnum", re.I)
_ARCH_MIN_SIDE, _ARCH_MIN_LONG = 700, 1000     # мельче — мыло в вертикали 1080×1920
_ARCH_CHECKS_PER_SLOT = 5                      # потолок vision-проверок на слот (время + квота Gemini)
_ARCH_DEAD: set = set()                        # источники, отказавшие в этом запуске (403/капча)
_VISION_FAILS = [0]                            # сбоев Gemini vision подряд; 3 → фото/хронику не берём (fail-closed)


def _free_license(short: str, url: str = "") -> str | None:
    """Нормализованное имя свободной лицензии или None. 'Public domain' | 'CC0' | 'CC BY 4.0' | 'CC BY-SA 3.0'."""
    s = f"{short or ''} {url or ''}".lower().replace("_", " ")
    if re.search(r"\b(nc|nd)\b|noncommercial|non-commercial|noderiv|no derivative|-nc|-nd|fair use", s):
        return None
    if "cc0" in s or "zero/1.0" in s:
        return "CC0"
    if re.search(r"public domain|publicdomain|\bpd\b|\bpd-|no known (copyright )?restrictions|no restrictions", s):
        return "Public domain"
    m = re.search(r"cc[ -]by(-sa)?(?:[ /-]?(\d\.\d)(?:[ /-]([a-z]{2,3})\b)?)?|"
                  r"creativecommons\.org/licenses/by(-sa)?/(\d\.\d)(?:/([a-z]{2,3})\b)?", s)
    if m:
        sa = m.group(1) or m.group(4)
        ver = m.group(2) or m.group(5) or ""
        jur = m.group(3) or m.group(6) or ""          # портированная лицензия (3.0 de) — указываем как есть
        if not jur:                                    # в коротком имени нет — берём из URL лицензии
            u = re.search(r"creativecommons\.org/licenses/by(?:-sa)?/\d\.\d/([a-z]{2,3})\b", s)
            jur = u.group(1) if u else ""
        return f"CC BY{'-SA' if sa else ''} {ver}{' ' + jur if jur and jur != 'en' else ''}".strip()
    return None


def _strip_html(s: str, limit: int = 120) -> str:
    s = re.sub(r'<span style="display: ?none;?">.*?</span>', " ", s or "", flags=re.S)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit]


_COMMONS_STD_W = (1920, 1280, 960)   # стандартные ширины превью Wikimedia (остальное и оригиналы → 429)


def _commons_thumb(ii: dict) -> str:
    """URL превью стандартной ширины: оригиналы ≤1920px Commons отдаёт «как есть», а их
    upload.wikimedia.org для ботов режет 429 — строим /thumb/…/<W>px- сами."""
    url = (ii.get("thumburl") or "").split("?")[0]
    if "/thumb/" in url:
        return url.replace("//upload.wikimedia.org/", "//thumb.wikimedia.org/", 1)
    orig = (ii.get("url") or url).split("?")[0]
    w = int(ii.get("width") or 0)
    std = next((x for x in _COMMONS_STD_W if x < w), 0)
    if not std or "/wikipedia/commons/" not in orig:
        return ""                                     # превью нет → оригинал с upload.* всё равно даст 429
    name = orig.rsplit("/", 1)[-1]
    thumb = (orig.replace("/wikipedia/commons/", "/wikipedia/commons/thumb/", 1)
             .replace("//upload.wikimedia.org/", "//thumb.wikimedia.org/", 1) + f"/{std}px-{name}")
    return thumb + (".jpg" if name.lower().endswith((".tif", ".tiff")) else "")


def _commons(query: str, used: set) -> list[dict]:
    """Wikimedia Commons: поиск файлов-картинок + extmetadata (лицензия/автор). Без ключа."""
    if "commons" in _ARCH_DEAD:
        return []
    url = "https://commons.wikimedia.org/w/api.php?" + urllib.parse.urlencode({
        "action": "query", "format": "json", "generator": "search",
        "gsrsearch": f"{query} filetype:bitmap", "gsrnamespace": 6, "gsrlimit": 20,
        "prop": "imageinfo", "iiprop": "url|size|extmetadata|mime", "iiurlwidth": 1920,   # только стандартные ширины превью, иначе upload.wikimedia.org → 429
        "iiextmetadatafilter": "LicenseShortName|LicenseUrl|Artist|Credit|ImageDescription|Restrictions|ObjectName",
    })
    data = _http_json(url, headers=_ARCH_UA)
    if not data:
        return []
    out = []
    for p in (data.get("query", {}) or {}).get("pages", {}).values():
        key = f"commons:{p.get('pageid')}"
        ii = (p.get("imageinfo") or [{}])[0]
        if key in used or not ii.get("thumburl") or not _commons_thumb(ii):
            continue
        m = {k: (v or {}).get("value", "") for k, v in (ii.get("extmetadata") or {}).items()}
        lic = _free_license(m.get("LicenseShortName", ""), m.get("LicenseUrl", ""))
        if not lic or "trademark" in (m.get("Restrictions") or "").lower():
            continue
        author = _strip_html(m.get("Artist", "")) or "Unknown author"
        credit = _strip_html(m.get("Credit", ""))
        if _ARCH_BAD_SRC.search(f"{author} {credit} {p.get('title', '')} {_strip_html(m.get('ImageDescription', ''), 2000)}"):
            continue
        out.append({"key": key, "source": "wikimedia_commons", "file_url": _commons_thumb(ii),
                    "page_url": ii.get("descriptionurl", ""), "title": p.get("title", "").removeprefix("File:"),
                    "author": author, "license": lic, "license_url": m.get("LicenseUrl", ""),
                    "w": ii.get("width", 0), "h": ii.get("height", 0), "kind": "image",
                    "text": f"{p.get('title', '')} {_strip_html(m.get('ImageDescription', ''))}",
                    "order": p.get("index", 99)})
    return out


def _loc(query: str, used: set) -> list[dict]:
    """Library of Congress (loc.gov JSON API): только «No known restrictions»/PD. Без ключа.
    С некоторых IP сайт отдаёт 403 (капча Cloudflare) — тогда источник выключается до конца запуска."""
    if "loc" in _ARCH_DEAD:
        return []
    url = "https://www.loc.gov/photos/?" + urllib.parse.urlencode({"q": query, "fo": "json", "c": 20})
    data = core.http_json(url, headers=_ARCH_UA, retries=1)
    if not data:
        _ARCH_DEAD.add("loc")
        return []
    out = []
    for r in data.get("results", []):
        rid = r.get("id", "")
        key = f"loc:{rid}"
        rights = " ".join(str(x) for x in (r.get("rights_advisory"), (r.get("item") or {}).get("rights_advisory"),
                                           r.get("rights")) if x)
        if key in used or "no known restrictions" not in rights.lower():
            continue
        imgs = [u for u in (r.get("image_url") or []) if isinstance(u, str) and u.startswith("http")]
        if not imgs:
            continue
        big = imgs[-1].split("#")[0]
        author = _strip_html(", ".join(r.get("contributor") or [])) or "Library of Congress"
        out.append({"key": key, "source": "library_of_congress", "file_url": big, "page_url": rid,
                    "title": _strip_html(r.get("title", "")), "author": author, "license": "Public domain",
                    "license_url": "https://www.loc.gov/legal/", "w": 0, "h": 0, "kind": "image",
                    "text": f"{r.get('title', '')} {' '.join(r.get('subject') or [])}", "order": len(out)})
    return out


_SPACE_RE = re.compile(r"\b(apollo|nasa|rocket|launch|astronaut|moon|lunar|orbit|satellite|space|shuttle|"
                       r"sputnik|gemini|mercury|mars|planet|telescope)\b", re.I)


def _nasa_images(query: str, used: set) -> list[dict]:
    """NASA Image Library (images-api.nasa.gov) — PD-фото NASA; только для космических сцен."""
    if not _SPACE_RE.search(query or "") or "nasa" in _ARCH_DEAD:
        return []
    data = _http_json("https://images-api.nasa.gov/search?" + urllib.parse.urlencode(
        {"q": query, "media_type": "image", "page_size": 15}), headers=_ARCH_UA)
    if not data:
        return []
    out = []
    for it in (data.get("collection", {}) or {}).get("items", [])[:15]:
        d = (it.get("data") or [{}])[0]
        nid = d.get("nasa_id")
        key = f"nasaimg:{nid}"
        who = f"{d.get('secondary_creator', '')} {d.get('photographer', '')}"
        if (not nid or key in used or _ARCH_BAD_SRC.search(who + " " + d.get("description", ""))
                or re.search(r"©|copyright|\besa\b|jaxa|roscosmos|courtesy of", f"{who} {d.get('description', '')}", re.I)):
            continue                                  # сторонний автор/© — не PD, мимо
        out.append({"key": key, "source": "nasa_images",
                    "file_url": f"https://images-assets.nasa.gov/image/{urllib.parse.quote(nid)}/{urllib.parse.quote(nid)}~large.jpg",
                    "page_url": f"https://images.nasa.gov/details/{urllib.parse.quote(nid)}",
                    "title": d.get("title", ""), "author": d.get("photographer") or d.get("center") or "NASA",
                    "license": "Public domain", "license_url": "https://www.nasa.gov/nasa-brand-center/images-and-media/",
                    "w": 0, "h": 0, "kind": "image", "text": f"{d.get('title', '')} {d.get('description', '')[:300]}",
                    "order": len(out)})
    return out


def _arch_queries(query: str) -> list[str]:
    """Запрос сцены → упрощения для архивного поиска (полный → без хвоста → 3 первых значимых слова)."""
    q = re.sub(r"\s+", " ", re.sub(r"[^\w\s'-]", " ", query or "")).strip()
    words = q.split()
    out = [q]
    if len(words) > 4:
        out.append(" ".join(words[:4]))
    stop = {"over", "with", "from", "into", "under", "during", "after", "before", "their", "about", "near"}
    sig = [w for w in words if (len(w) >= 4 or w.isdigit()) and w.lower() not in stop]
    if len(sig) >= 2:
        out.append(" ".join(sig[:3]))
    names = [w for w in words if w[:1].isupper() or w.isdigit()]   # имена/места/годы — костяк архивного поиска
    if len(names) >= 2:
        out.append(" ".join(names[:3]))
    seen, res = set(), []
    for x in out:
        if x and x.lower() not in seen:
            seen.add(x.lower())
            res.append(x)
    return res


def _arch_frame(src: pathlib.Path, out: pathlib.Path, focus_x: float = 0.5) -> str | None:
    """Фото → вертикальный кадр с запасом под Ken Burns (assemble._big_wh): размытая заливка
    из того же фото + само фото по ширине (широкие кадрируются до ~1.1:1 вокруг объекта).
    Портретные/квадратные документы и горизонтальный формат long — кроп-cover по центру интереса."""
    from PIL import Image, ImageFilter, ImageOps, ImageEnhance
    try:
        im = ImageOps.exif_transpose(Image.open(src)).convert("RGB")
    except Exception:  # noqa: BLE001
        return None
    bw, bh = int(core.W * 1.15), int(core.H * 1.15)
    im = ImageOps.autocontrast(im, cutoff=1)

    def cover(img, w, h, fx):
        s = max(w / img.width, h / img.height)
        img = img.resize((max(w, int(img.width * s)), max(h, int(img.height * s))), Image.LANCZOS)
        x = int(min(max(fx * img.width - w / 2, 0), img.width - w))
        y = (img.height - h) // 2
        return img.crop((x, y, x + w, y + h))

    a = im.width / im.height
    if _landscape() or abs(a - bw / bh) < 0.08:
        canvas = cover(im, bw, bh, focus_x)
    else:
        bg = cover(im, bw, bh, 0.5).filter(ImageFilter.GaussianBlur(40))
        canvas = ImageEnhance.Brightness(bg).enhance(0.45)
        fa = min(a, 1.1)                                 # широкие — до ~1.1:1 вокруг объекта
        if fa < a:
            cw = int(im.height * fa)
            x = int(min(max(focus_x * im.width - cw / 2, 0), im.width - cw))
            im = im.crop((x, 0, x + cw, im.height))
        fw = bw
        fh = int(fw / (im.width / im.height))
        if fh > int(bh * 0.8):                            # высокие документы — по высоте 80%
            fh = int(bh * 0.8)
            fw = int(fh * im.width / im.height)
        fg = im.resize((fw, fh), Image.LANCZOS)
        canvas.paste(fg, ((bw - fw) // 2, int((bh - fh) * 0.42)))
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out, quality=92)
    return str(out)


def _arch_photo(cand: dict, scene: str, niche: dict, workdir: pathlib.Path, i: int, narration: str = "") -> dict | None:
    """Скачать кандидата → разрешение → vision-проверка → вертикальный кадр. None — брак."""
    from pipeline import imagegen
    ext = ".png" if cand["file_url"].lower().endswith(".png") else ".jpg"
    dest = core.CACHE_DIR / f"arch_{hashlib.md5(cand['file_url'].encode()).hexdigest()[:16]}{ext}"
    if not _download_arch(cand["file_url"], dest):
        return None
    try:
        from PIL import Image
        with Image.open(dest) as im:
            w, h = im.size
    except Exception:  # noqa: BLE001
        return None
    ow, oh = cand.get("w") or w, cand.get("h") or h            # у Commons — размер оригинала
    if (min(ow, oh) < _ARCH_MIN_SIDE or max(ow, oh) < _ARCH_MIN_LONG or min(w, h) < 600
            or not 0.45 <= w / h <= 2.6):                # газетная полоса/панорама — в вертикали нечитаемы
        core.log("archival: мелкий кадр — мимо", level="debug", slot=i, size=f"{ow}x{oh}", url=cand["page_url"])
        return None
    chk = imagegen.archival_check(str(dest), scene, niche.get("frame_rules"), caption=_strip_html(cand["text"], 400),
                                  narration=narration)
    if not chk["checked"] and not os.environ.get("GEMINI_API_KEY", "").strip():
        _ARCH_DEAD.add("vision")                  # ключей нет вовсе — повтор бессмыслен, дальше только карты
    elif not chk["checked"]:                     # кратковременный 429/503 — одна повторная попытка через паузу
        time.sleep(15)
        chk = imagegen.archival_check(str(dest), scene, niche.get("frame_rules"),
                                      caption=_strip_html(cand["text"], 400), narration=narration)
    if not chk["checked"]:                       # Gemini недоступен → fail-closed: непроверенное фото может
        _vision_fail()                           # оказаться graphic (трупы/кровь) — в archival не берём
        core.log("archival: vision недоступен — фото не проверить", level="warn", slot=i)
        return None
    _VISION_FAILS[0] = 0
    if not chk["ok"]:
        core.log("archival: кадр забракован", level="info", slot=i, issues=chk["issues"],
                 subject=chk.get("subject", ""), url=cand["page_url"])
        return None
    frame = _arch_frame(dest, workdir / f"arch_{i:02d}.jpg", chk.get("focus_x", 0.5))
    if not frame:
        return None
    return {"path": frame, "kind": "image", "source": cand["source"], "source_url": cand["page_url"],
            "file_url": cand["file_url"], "title": cand["title"], "author": cand["author"],
            "license": cand["license"], "license_url": cand["license_url"],
            "relevance": chk.get("relevance"), "subject": chk.get("subject", ""), "key": cand["key"]}


def _vision_fail() -> None:
    _VISION_FAILS[0] += 1
    if _VISION_FAILS[0] >= 3:
        _ARCH_DEAD.add("vision")
        core.log("archival: vision недоступен 3 раза подряд — дальше только карты", level="warn")


def _download_arch(url: str, dest: pathlib.Path) -> bool:
    if dest.exists() and dest.stat().st_size > 10_000:
        return True
    return core.http_download(url, dest, headers=_ARCH_UA, timeout=90, min_bytes=10_000)


def _arch_video(query: str, scene: str, niche: dict, workdir: pathlib.Path, i: int, dur: float,
                used: set, narration: str = "") -> dict | None:
    """Хроника Internet Archive (PD / CC BY, _archive_org) → нарезка куска dur → vision-проверка
    кадра из середины куска (2 смещения на фильм). Кусок ложится как обычный видео-клип.
    Зеркала YouTube-роликов (identifier youtube-*) мимо: чья плёнка — неизвестно, риск Content ID."""
    from pipeline import imagegen
    if "archive" in _ARCH_DEAD:
        return None
    res = _archive_org(query, used, core.CACHE_DIR, extra=" AND NOT identifier:youtube-*")
    if not res:
        return None
    path, page = res
    ident = page.rsplit("/", 1)[-1]
    if ident.lower().startswith("youtube-"):
        return None
    meta = (_http_json(f"https://archive.org/metadata/{ident}") or {}).get("metadata", {}) or {}
    lic = _free_license("", meta.get("licenseurl", ""))
    if not lic or lic.startswith("CC BY-SA"):                  # _archive_org пропускает только PD / CC BY
        return None
    total = core.media_duration(path)
    if total < dur + 4:
        return None
    for k, frac in enumerate((0.35, 0.65)):
        off = max(1.0, min(total * frac, total - dur - 1.0))
        clip = workdir / f"archv_{i:02d}_{k}.mp4"
        probe = workdir / f"archv_{i:02d}_{k}.jpg"
        try:
            core.run(["ffmpeg", "-y", "-ss", f"{off:.2f}", "-i", str(path), "-t", f"{dur + 0.3:.2f}", "-an",
                      "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", str(clip)])
            core.run(["ffmpeg", "-y", "-ss", f"{dur / 2:.2f}", "-i", str(clip), "-frames:v", "1", str(probe)])
        except Exception as e:  # noqa: BLE001
            core.log_error("broll.arch_video", e, slot=i)
            continue
        chk = imagegen.archival_check(str(probe), scene, niche.get("frame_rules"),
                                      caption=_strip_html(str(meta.get("title", "")), 400), narration=narration)
        if not chk["checked"]:                   # fail-closed, как у фото
            _vision_fail()
            return None
        _VISION_FAILS[0] = 0
        if chk["ok"]:
            author = _strip_html(meta.get("creator", "")) if isinstance(meta.get("creator"), str) else "Internet Archive"
            return {"path": str(clip), "kind": "stock", "source": "archive_org", "source_url": page,
                    "file_url": page, "title": _strip_html(meta.get("title", ident) if isinstance(meta.get("title"), str) else ident),
                    "author": author or "Internet Archive", "license": lic,
                    "license_url": meta.get("licenseurl", ""), "relevance": chk.get("relevance"),
                    "subject": chk.get("subject", ""), "key": f"archive:{ident}"}
        core.log("archival: кусок хроники забракован", level="info", slot=i, issues=chk["issues"], url=page)
    return None


def _arch_map(spec: str, niche: dict, workdir: pathlib.Path, i: int, variant: int = 0) -> dict | None:
    from pipeline import maps
    pal = niche.get("palette") or [["0a0a14", "e4572e"]]
    accent = pal[0][1] if len(pal[0]) > 1 else "e4572e"
    path = maps.render(spec, workdir / f"map_{i:02d}.png", int(core.W * 1.15), int(core.H * 1.15), accent=accent,
                       variant=variant)
    if not path:
        return None
    return {"path": path, "kind": "image", "source": "natural_earth_map", "source_url": maps.NE_PAGE,
            "file_url": maps.NE_PAGE, "title": spec, "author": "Natural Earth (map drawn in code)",
            "license": "Public domain", "license_url": "https://www.naturalearthdata.com/about/terms-of-use/"}


def _archival_slot(query: str, niche: dict, workdir: pathlib.Path, i: int, dur: float, used: set,
                   state: dict, narration: str = "") -> dict:
    """Один слот archival: MAP:-сцена → карта; иначе фото (Commons → LoC → NASA) с vision-проверкой,
    каждый video_every-й слот сначала пробует хронику; ничего не прошло → карта по стране из
    текста сцены / последней карте ролика / niche.map_default; совсем ничего → градиент."""
    from pipeline import maps
    drawn = state.setdefault("maps_drawn", {})        # спецификация (нижний регистр) → сколько раз нарисована
    if maps.parse_spec(query):
        spec = query
        if not maps.find_region(maps.parse_spec(query)["regions"][0]):   # «MAP: Russian America 1867»
            spec = maps.spec_from_text(re.sub(r"^\s*map\s*:\s*", "", query, flags=re.I)) or query
        state["last_map"] = spec
        text_q = re.sub(r"^\s*map\s*:\s*", "", query, flags=re.I).replace("->", " ")
        if not drawn.get(maps.canonical(spec)) and (item := _arch_map(spec, niche, workdir, i)):
            drawn[maps.canonical(spec)] = 1
            return item
        query = text_q                                # та же карта уже была — сначала ищем фото места
    every = int(niche.get("archival_video_every", 4) or 0) if "vision" not in _ARCH_DEAD and not _VISION_FAILS[0] else 0
    want_video = bool(every) and i > 0 and i % every == 0
    if want_video:
        item = _arch_video(query, query, niche, workdir, i, dur, used, narration)
        if item:
            return item
    checks = 0 if "vision" not in _ARCH_DEAD else _ARCH_CHECKS_PER_SLOT
    for q in _arch_queries(query):
        if checks >= _ARCH_CHECKS_PER_SLOT or "vision" in _ARCH_DEAD:
            break                                # vision мёртв — поиск всё равно некому проверить
        cands = _commons(q, used) + _loc(q, used) + _nasa_images(q, used)
        qt = _qtok(query)
        cands.sort(key=lambda c: (-_overlap(qt, c["text"]), c["order"]))   # ближе к сцене — раньше
        for c in cands:
            if checks >= _ARCH_CHECKS_PER_SLOT or "vision" in _ARCH_DEAD:
                break
            if c["key"] in used or c["key"] in state.setdefault("rejected", set()):
                continue
            checks += 1
            item = _arch_photo(c, query, niche, workdir, i, narration)
            if item:
                used.add(c["key"])
                return item
            state["rejected"].add(c["key"])  # брак под эту сцену — в этом ролике больше не проверяем
        if checks >= _ARCH_CHECKS_PER_SLOT or "vision" in _ARCH_DEAD:
            break
    if not want_video and "vision" not in _ARCH_DEAD and not _VISION_FAILS[0]:
        item = _arch_video(query, query, niche, workdir, i, dur, used, narration)
        if item:
            return item
    spec = maps.spec_from_text(query) or state.get("last_map") or state.get("last_fallback_map") or (
        f"MAP: {niche['map_default']}" if niche.get("map_default") else None)
    if spec:
        key = maps.canonical(spec)
        item = _arch_map(spec, niche, workdir, i, variant=drawn.get(key, 0))
        if item:
            drawn[key] = drawn.get(key, 0) + 1
            state["last_fallback_map"] = spec
            core.log("archival: кадра нет — карта", level="info", slot=i, q=query, map=spec)
            return item
    return {"path": _gen_gradient(niche.get("palette", []), i, dur, workdir / f"bg_{i:02d}.mp4"),
            "kind": "gen", "source": "generated", "source_url": "", "query": query}


def archival_sources(clips: list[dict]) -> list[dict]:
    """Журнал источников для sources.json: кадр → страница, автор, лицензия."""
    out = []
    t = 0.0
    for k, c in enumerate(clips):
        if c.get("license"):
            out.append({"slot": k, "start": round(t, 2), "dur": round(c.get("dur", 0), 2),
                        "source": c["source"], "page_url": c.get("source_url", ""),
                        "file_url": c.get("file_url", ""), "title": c.get("title", ""),
                        "author": c.get("author", ""), "license": c["license"],
                        "license_url": c.get("license_url", ""), "query": c.get("query", ""),
                        "subject": c.get("subject", ""), "relevance": c.get("relevance"),
                        "reused": bool(c.get("reused"))})
        t += c.get("dur", 0)
    return out


def attribution_line(sources: list[dict]) -> str:
    """Строка атрибуции для описания — только для CC BY / CC BY-SA (PD/CC0 её не требуют)."""
    names = {"wikimedia_commons": "Wikimedia Commons", "archive_org": "Internet Archive"}
    parts, seen = [], set()
    for s in sources:
        if not s["license"].startswith("CC BY") or s["page_url"] in seen:
            continue
        seen.add(s["page_url"])
        parts.append(f"{names.get(s['source'], s['source'])} — {s['author']}, {s['license']} ({s['page_url']})")
    return ("Images: " + "; ".join(parts)) if parts else ""


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
    tries = 3 if niche.get("frame_qa") else 1
    img = None
    for t in range(tries):   # frame_qa: брак кадра (артефакты/текст/не та сцена) → новый seed
        img = imagegen.generate(query, niche, workdir / f"img_{i:02d}.png",
                                seed=_img_seed(workdir, i) + t * 1000, character=character)
        if not img or tries == 1:
            break
        chk = imagegen.frame_check(img, niche, query)
        if chk["ok"]:
            break
        core.log("frame_qa: брак кадра, перегенерация", level="warn", slot=i, attempt=t, issues=chk["issues"][:3])
        if t == tries - 1:
            img = None   # все попытки брак → слот уйдёт в сток-фолбэк fetch_for
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
    | 'archival' (настоящие архивные фото/хроника со свободной лицензией + карты кодом, см. _archival_slot)
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
    era_ai_frames: list[dict] = []   # успешные AI-кадры эпохо-слотов — пул для повтора при исчерпании квот
    arch_state: dict = {}            # archival: последняя карта ролика (фолбэк для слотов без кадра)
    if mode == "archival":           # сбои vision прошлого ролика этого процесса не переносим на новый
        _ARCH_DEAD.discard("vision")
        _VISION_FAILS[0] = 0

    cursor = 0.0
    for i, dur in enumerate(slot_plan):
        query = _query_at(timed_chunks, cursor + dur / 2, fallback_q)
        if is_long:
            # long: запрос из семейства главы (ротация внутри семейства, батчинг Pexels-кэшем)
            search_query = _long_query(fams, cursor + dur / 2, fam_rot, query)
        elif i < len(INTRO_SLOTS) and mode == "archival":
            # интро: запросы РАЗНЫХ сегментов (контраст кадров), но без модификаторов плана —
            # «close up» ломает архивный поиск и MAP:-спецификации
            qs = [q for q in ((c.get("broll_query") or "").strip() for c in timed_chunks) if q]
            search_query = qs[i] if len(qs) > i else query
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
        if mode == "archival":                            # настоящие фото/хроника/карты, без стока и AI
            narration = next((c.get("text", "") for c in timed_chunks
                              if c["start"] <= cursor + dur / 2 < c["end"]), "")
            item = _archival_slot(search_query, niche, workdir, i, dur, used, arch_state, narration)
            item.setdefault("query", search_query)
            item["dur"] = dur
            out.append(item)
            cursor += dur
            continue
        # mixed: этот слот — AI-картинка? Равномерное распределение доли ai_ratio по таймлайну,
        # интро (первые LONG_INTRO_SLOTS) не трогаем — КРОМЕ эпохо-запросов. Эпохо-специфичный
        # запрос (год/эра) идёт в AI ВНЕ квоты ratio и даже в интро: стока 1860-х не существует,
        # Pexels отдаёт современный город в джинсах — анахронизм в первых секундах хуже всего
        # (дневной лимит NVIDIA всё равно гейтит в _ai_image_slot: item=None → era-фолбэк ниже).
        era_q = bool(_ERA_RE.search(search_query))
        ai_slot = (mode == "mixed"
                   and (era_q or (i >= len(LONG_INTRO_SLOTS)
                                  and ai_done < ai_ratio * (i + 1 - len(LONG_INTRO_SLOTS)))))
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
                if era_q:
                    era_ai_frames.append(dict(item))

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

        if item is None and era_q:
            # эпохо-слот остался без AI-кадра (квоты NVIDIA/Pollinations исчерпаны или сбой):
            # современный сток под нарратив 1860-х = анахронизм (крипто-терминал в видео про
            # Рокфеллера — реальный случай). Сначала настоящая архивная плёнка (Internet
            # Archive, PD), затем повтор уже сгенерированного AI-кадра эпохи — панорама
            # в assemble идёт другим направлением, повтор мягче анахронизма.
            res = _archive_org(search_query, used, core.CACHE_DIR)
            if res:
                path, src_url = res
                downloaded.append((path, src_url))
                item = {"path": path, "kind": "stock", "source": "archive_org",
                        "source_url": src_url, "query": query}
            elif era_ai_frames:
                prev = era_ai_frames[i % len(era_ai_frames)]
                item = {**prev, "query": query}
                core.log("broll era reuse", level="debug", slot=i, from_q=prev.get("query"), q=query)

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
