"""Карты КОДОМ (не нейросетью) по открытым данным Natural Earth (public domain).

Сцена «где/границы» в сценарии приходит как broll_query вида
  "MAP: Vietnam"                     — подсветить страну
  "MAP: Alaska, Russia"              — подсветить несколько стран/регионов
  "MAP: Russia -> Alaska"            — подсветить оба и провести стрелку от первого ко второму
Ортографическая проекция («глобус») с центром на подсвеченном, без подписей и флагов —
надписи на картах путают зрителя и ловятся QA как мусор.

  python3 -m pipeline.maps "MAP: Russia -> Alaska" out.png
"""
import json
import math
import pathlib
import re

import sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import core  # noqa: E402

_NE_BASE = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/"
_NE_FILES = {"admin0": "ne_50m_admin_0_countries.geojson",
             "admin1": "ne_50m_admin_1_states_provinces.geojson"}
NE_PAGE = "https://www.naturalearthdata.com/"

# исторические/разговорные имена → имя Natural Earth
_ALIASES = {
    "usa": "united states of america", "us": "united states of america", "u.s.": "united states of america",
    "united states": "united states of america", "america": "united states of america",
    "soviet union": "russia", "ussr": "russia", "russian empire": "russia",
    "uk": "united kingdom", "britain": "united kingdom", "great britain": "united kingdom",
    "england": "united kingdom", "north vietnam": "vietnam", "south vietnam": "vietnam",
    "burma": "myanmar", "persia": "iran", "siam": "thailand",
    "ottoman empire": "turkey", "prussia": "germany", "west germany": "germany", "east germany": "germany",
    "czechoslovakia": "czechia", "yugoslavia": "serbia", "zaire": "dem. rep. congo",
    "holland": "netherlands", "congo": "dem. rep. congo",
    # столицы/города из архивных запросов → страна (для карты-фолбэка)
    "saigon": "vietnam", "hanoi": "vietnam", "moscow": "russia", "st. petersburg": "russia",
    "washington": "united states of america", "new york": "united states of america",
    "berlin": "germany", "london": "united kingdom", "paris": "france", "beijing": "china",
    "peking": "china", "tokyo": "japan", "havana": "cuba", "seoul": "south korea", "sitka": "alaska",
    "british north america": "canada", "new france": "canada", "russian america": "alaska", "alaska territory": "alaska", "indochina": "vietnam",
    "french indochina": "vietnam",
}

_DATA: dict[str, list] = {}


def _load(kind: str) -> list:
    """Фичи Natural Earth (кэш в CACHE_DIR/natural_earth; подпапку cleanup_cache не трогает)."""
    if kind in _DATA:
        return _DATA[kind]
    d = core.CACHE_DIR / "natural_earth"
    d.mkdir(parents=True, exist_ok=True)
    f = d / _NE_FILES[kind]
    if not (f.exists() and f.stat().st_size > 100_000):
        if not core.http_download(_NE_BASE + _NE_FILES[kind], f, timeout=120, min_bytes=100_000):
            _DATA[kind] = []
            return []
    _DATA[kind] = json.loads(f.read_text(encoding="utf-8")).get("features", [])
    return _DATA[kind]


def _names(feat: dict, kind: str) -> set:
    p = feat.get("properties", {})
    keys = (("NAME", "NAME_LONG", "ADMIN", "NAME_EN", "FORMAL_EN", "GEOUNIT", "BRK_NAME") if kind == "admin0"
            else ("name", "name_en", "gn_name"))
    return {str(p.get(k) or "").strip().lower() for k in keys if p.get(k)}


def find_region(name: str) -> dict | None:
    """Страна (admin-0) или регион/штат (admin-1) по имени → {name, polys[[(lon,lat)...]], label}."""
    q = re.sub(r"\b(?:\d{3,4}s?|\d{1,2}(?:st|nd|rd|th) century)\b", " ", (name or "").lower())   # годы/века мешают
    q = re.sub(r"\s+", " ", q).strip(" .")
    q = _ALIASES.get(q, q)
    if len(q) < 2:
        return None
    for kind in ("admin0", "admin1"):
        feats = _load(kind)
        hit = next((f for f in feats if q in _names(f, kind)), None)
        if hit is None and kind == "admin1":   # мелкие регионы ищем только точным совпадением
            continue
        if hit is None and len(q) >= 5:
            hit = next((f for f in feats if any(n.startswith(q) for n in _names(f, kind))), None)
        if hit is None:
            continue
        g = hit.get("geometry") or {}
        rings = ([g["coordinates"][0]] if g.get("type") == "Polygon"
                 else [poly[0] for poly in g.get("coordinates", [])] if g.get("type") == "MultiPolygon" else [])
        if not rings:
            continue
        p = hit["properties"]
        lx = p.get("LABEL_X", p.get("longitude"))
        ly = p.get("LABEL_Y", p.get("latitude"))
        if lx is None or ly is None:          # центр самого большого кольца
            big = max(rings, key=len)
            lx = sum(c[0] for c in big) / len(big)
            ly = sum(c[1] for c in big) / len(big)
        return {"name": q, "polys": rings, "label": (float(lx), float(ly))}
    return None


def known_names() -> list[str]:
    """Все имена стран (для поиска упоминания страны в тексте сцены)."""
    out = set(_ALIASES)
    for f in _load("admin0"):
        out |= {n for n in _names(f, "admin0") if len(n) >= 4}
    for f in _load("admin1"):                    # штаты/провинции США и Канады (Alaska, Texas, Quebec…)
        if (f.get("properties") or {}).get("iso_a2") in ("US", "CA"):
            out |= {n for n in _names(f, "admin1") if len(n) >= 5}
    return sorted(out, key=len, reverse=True)


def parse_spec(query: str) -> dict | None:
    """'MAP: A -> B' / 'MAP: A, B' → {"regions": [...], "arrow": (A, B) | None}. Не карта → None."""
    m = re.match(r"\s*map\s*:\s*(.+)$", query or "", re.I)
    if not m:
        return None
    body = m.group(1)
    arrow = None
    if "->" in body:
        a, b = [x.strip() for x in body.split("->", 1)]
        a, b = a.split(",")[0].strip(), b.split(",")[0].strip()
        arrow = (a, b)
        regions = [a, b]
    else:
        regions = [x.strip() for x in body.split(",") if x.strip()]
    return {"regions": regions[:4], "arrow": arrow}


_AMBIGUOUS = {"america", "us", "u.s.", "korea", "congo", "usa"}   # «North America», «us» — не повод для карты США


def spec_from_text(text: str) -> str | None:
    """Страны/регионы, упомянутые в тексте сцены (до двух, по порядку упоминания) → 'MAP: A, B'
    (фолбэк, когда архива нет). Длинные имена ищутся первыми и «съедают» текст, чтобы
    «British Columbia» не дала заодно «Columbia»."""
    low = f" {(text or '').lower()} "
    hits = []
    for n in known_names():
        if n in _AMBIGUOUS:
            continue
        m = re.search(rf"(?<![a-z]){re.escape(n)}(?![a-z])", low)
        if m:
            hits.append((m.start(), n))
            low = low[:m.start()] + " " * len(n) + low[m.end():]
    if not hits:
        return None
    names = [n for _, n in sorted(hits)]
    regions = []
    for n in names:                                   # разные имена одного региона (alaska / russian america)
        r = find_region(n)
        if r and r["name"] not in [x[1] for x in regions]:
            regions.append((n, r["name"]))
    return "MAP: " + ", ".join(n for n, _ in regions[:2]) if regions else f"MAP: {names[0]}"


def canonical(query: str) -> str:
    """Ключ карты по РЕГИОНАМ, а не по тексту: «MAP: saigon» и «MAP: Vietnam» — одна и та же карта."""
    spec = parse_spec(query)
    if not spec:
        return (query or "").strip().lower()
    names = [(find_region(n) or {}).get("name") or n.strip().lower() for n in spec["regions"]]
    return ("->" if spec["arrow"] else ",").join(names if spec["arrow"] else sorted(names))


# ───────── проекция ─────────

def _unit(lon: float, lat: float) -> tuple[float, float, float]:
    lo, la = math.radians(lon), math.radians(lat)
    return math.cos(la) * math.cos(lo), math.cos(la) * math.sin(lo), math.sin(la)


def _center(regions: list[dict]) -> tuple[float, float]:
    """Центр вида = среднее единичных векторов точек (корректно через линию перемены дат)."""
    sx = sy = sz = 0.0
    if len(regions) > 1:                         # несколько регионов — каждый с равным весом (по метке),
        for r in regions:                        # иначе островная страна перетянет центр и второй уйдёт за край
            x, y, z = _unit(*r["label"])
            sx, sy, sz = sx + x, sy + y, sz + z
        regions = []
    for r in regions:
        for ring in r["polys"]:
            for lon, lat in ring[::max(1, len(ring) // 60)]:
                x, y, z = _unit(lon, lat)
                sx, sy, sz = sx + x, sy + y, sz + z
    n = math.sqrt(sx * sx + sy * sy + sz * sz) or 1.0
    return math.degrees(math.atan2(sy, sx)), math.degrees(math.asin(max(-1.0, min(1.0, sz / n))))


def _ortho(lon0: float, lat0: float):
    l0, p0 = math.radians(lon0), math.radians(lat0)
    sp0, cp0 = math.sin(p0), math.cos(p0)

    def f(lon: float, lat: float) -> tuple[float, float, bool]:
        lo, la = math.radians(lon) - l0, math.radians(lat)
        cl = math.cos(la)
        x = cl * math.sin(lo)
        y = cp0 * math.sin(la) - sp0 * cl * math.cos(lo)
        vis = sp0 * math.sin(la) + cp0 * cl * math.cos(lo) >= 0
        if not vis:                              # обратная сторона → прижать к краю диска
            r = math.hypot(x, y) or 1.0
            x, y = x / r, y / r
        return x, -y, vis
    return f


def render(query: str, out: pathlib.Path, w: int, h: int, accent: str = "e4572e", variant: int = 0) -> str | None:
    """Нарисовать карту по спецификации MAP: … в PNG w×h. None — регион не найден/нет данных.
    variant нечётный — общий план (весь глобус) вместо крупного: повтор той же карты в ролике не выглядит копией."""
    from PIL import Image, ImageDraw, ImageFilter
    spec = parse_spec(query)
    if not spec:
        return None
    regions = [r for r in (find_region(n) for n in spec["regions"]) if r]
    if not regions:
        return None
    lon0, lat0 = _center(regions)
    v = variant % 4                               # 0 крупно, 1 глобус, 2 шире + поворот, 3 глобус с другой стороны
    lon0 += (0, 35, 18, -30)[v]
    lat0 = max(-80.0, min(80.0, lat0 + (0, -15, -8, 12)[v]))
    proj = _ortho(lon0, lat0)
    # масштаб: подсвеченное занимает ~55% ширины, но глобус не меньше 0.42 ширины кадра
    xs, ys = [], []
    for r in regions:
        for ring in r["polys"]:
            for lon, lat in ring:
                x, y, vis = proj(lon, lat)
                if vis:
                    xs.append(x)
                    ys.append(y)
    ext = max((max(xs) - min(xs)) if xs else 2.0, ((max(ys) - min(ys)) if ys else 2.0) * w / h * 0.75, 0.05)
    R = (max(0.42 * w, min(0.55 * w / ext, 6.0 * w)) * (1.0, 1.0, 0.7, 1.0)[v]) if v in (0, 2) else (0.42, 0.42, 0, 0.36)[v] * w
    cx0 = (max(xs) + min(xs)) / 2 if xs and R > 0.6 * w else 0.0
    cy0 = (max(ys) + min(ys)) / 2 if ys and R > 0.6 * w else 0.0
    ss = 2                                        # суперсэмплинг под сглаживание
    W, H = w * ss, h * ss
    ox, oy = W / 2 - cx0 * R * ss, H * 0.47 - cy0 * R * ss

    def pt(lon, lat):
        x, y, _v = proj(lon, lat)
        return ox + x * R * ss, oy + y * R * ss

    acc = tuple(int(accent.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
    img = Image.new("RGB", (W, H), (9, 14, 24))
    dr = ImageDraw.Draw(img)
    rr = R * ss
    # ореол атмосферы + океан
    glow = Image.new("RGB", (W, H), (0, 0, 0))
    ImageDraw.Draw(glow).ellipse([ox - rr * 1.04, oy - rr * 1.04, ox + rr * 1.04, oy + rr * 1.04], fill=(40, 90, 140))
    img = Image.blend(img, glow.filter(ImageFilter.GaussianBlur(30 * ss)), 0.55)
    dr = ImageDraw.Draw(img)
    dr.ellipse([ox - rr, oy - rr, ox + rr, oy + rr], fill=(18, 36, 58))
    # сетка 15°
    for lat in range(-75, 90, 15):
        line = [pt(lon, lat) for lon in range(-180, 181, 3) if proj(lon, lat)[2]]
        if len(line) > 1:
            dr.line(line, fill=(30, 54, 82), width=ss)
    for lon in range(-180, 180, 15):
        line = [pt(lon, lat) for lat in range(-88, 89, 2) if proj(lon, lat)[2]]
        if len(line) > 1:
            dr.line(line, fill=(30, 54, 82), width=ss)
    def draw_ring(ring, fill, outline, width):
        vis = [proj(lon, lat)[2] for lon, lat in ring]
        if not any(vis):
            return
        pts = [pt(lon, lat) for lon, lat in ring]
        if len(pts) > 2:
            dr.polygon(pts, fill=fill, outline=outline, width=width)

    for f in _load("admin0"):                     # вся суша приглушённо
        g = f.get("geometry") or {}
        rings = ([g["coordinates"][0]] if g.get("type") == "Polygon"
                 else [p[0] for p in g.get("coordinates", [])] if g.get("type") == "MultiPolygon" else [])
        for ring in rings:
            draw_ring(ring, (58, 70, 86), (88, 104, 124), ss)
    for k, r in enumerate(regions):               # подсветка: первый — акцент, остальные — светлее
        col = acc if k == 0 else tuple(min(255, int(c * 0.55 + 255 * 0.35)) for c in acc)
        for ring in r["polys"]:
            draw_ring(ring, col, (255, 244, 230), 2 * ss)
    if spec["arrow"]:
        a = next((r for r in regions if r["name"] == _ALIASES.get(spec["arrow"][0].lower(), spec["arrow"][0].lower())), regions[0])
        b = next((r for r in regions if r["name"] == _ALIASES.get(spec["arrow"][1].lower(), spec["arrow"][1].lower())), regions[-1])
        if a is not b:
            _arrow(dr, pt(*a["label"]), pt(*b["label"]), ss)
    else:                                         # маркер на подсвеченном регионе
        x, y = pt(*regions[0]["label"])
        for rad, col in ((22, (255, 255, 255)), (14, acc)):
            dr.ellipse([x - rad * ss, y - rad * ss, x + rad * ss, y + rad * ss], fill=col)
    img = img.resize((w, h), Image.LANCZOS)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return str(out)


def _arrow(dr, p0, p1, ss: int) -> None:
    """Дуга-стрелка p0 → p1 (квадратичная Безье с изгибом вверх) + наконечник."""
    (x0, y0), (x1, y1) = p0, p1
    dx, dy = x1 - x0, y1 - y0
    dist = math.hypot(dx, dy) or 1.0
    mx, my = (x0 + x1) / 2 - dy * 0.25, (y0 + y1) / 2 + dx * 0.25
    if my > (y0 + y1) / 2:                        # изгиб всегда вверх — читается как «перелёт»
        mx, my = (x0 + x1) / 2 + dy * 0.25, (y0 + y1) / 2 - dx * 0.25
    pts = []
    for i in range(41):
        t = i / 40 * (1 - 34 * ss / dist)         # не заходить линией под наконечник
        pts.append(((1 - t) ** 2 * x0 + 2 * (1 - t) * t * mx + t * t * x1,
                    (1 - t) ** 2 * y0 + 2 * (1 - t) * t * my + t * t * y1))
    dr.line(pts, fill=(20, 20, 20), width=16 * ss, joint="curve")
    dr.line(pts, fill=(255, 255, 255), width=9 * ss, joint="curve")
    ex, ey = pts[-1]
    ang = math.atan2(y1 - ey, x1 - ex)
    size = 40 * ss
    tip = (ex + math.cos(ang) * size, ey + math.sin(ang) * size)
    left = (ex + math.cos(ang + 2.3) * size * 0.6, ey + math.sin(ang + 2.3) * size * 0.6)
    right = (ex + math.cos(ang - 2.3) * size * 0.6, ey + math.sin(ang - 2.3) * size * 0.6)
    dr.polygon([tip, left, right], fill=(255, 255, 255), outline=(20, 20, 20), width=3 * ss)


if __name__ == "__main__":
    print(render(sys.argv[1], pathlib.Path(sys.argv[2]), 1242, 2208))
