"""Парсер трендов — мульти-источник, БЕСПЛАТНО и БЕЗ ключей (этап 1-2 пайплайна).

Собирает «что сейчас цепляет» по нише из нескольких бесплатных источников:
  • Google Trends RSS   — общие дневные тренды страны (geo: US для en, RU для ru)
  • Google News RSS     — новости по ключевикам ниши (главный нишевой источник)
  • Reddit .json        — топ постов из сабреддитов ниши (идеи/формулировки, особенно EN)
  • Wikimedia Pageviews — топ читаемых статей (en.wikipedia для EN-ниш)
  • VK/Telegram/ru-wiki — ТОЛЬКО для lang=ru ниш (для EN автоматически пропускаются)

Возвращает список кандидатов: {title, source, url, ts, weight}. Дальше selector.py
скорит, отсеивает дубли и через LLM-каскад превращает в готовые темы видео.
"""
import re
import json
import time
import urllib.parse
import urllib.request
import urllib.error
import datetime as dt
import xml.etree.ElementTree as ET

import sys, pathlib  # noqa: E401
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import core  # noqa: E402

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"

# Конфиг трендов по КАТЕГОРИЯМ ниш (ключевики для News/Suggest + сабреддиты). EN-фабрика:
# единственная трендовая категория — history (бизнес-империи, состояния, инженерия, быт-экономика).
# ВАЖНО: niche["category"] в niches.json — это YouTube categoryId (27=Education), НЕ ключ сюда;
# маппинг ниша→трендовая категория живёт в _NICHE_CATEGORY, _cat() это учитывает.
NICHE_TRENDS = {
    "history": {
        "en": ["history", "ancient rome", "industrial revolution", "gold rush",
               "great depression", "silk road", "engineering disasters",
               "business empire history", "lost fortune history"],
        "subreddits": ["history", "AskHistorians", "todayilearned"],
    },
}
_NICHE_CATEGORY = {
    "history_docs": "history", "history_shorts": "history",
}

# вес источника (приоритет сигнала)
WEIGHT = {"trends_mcp": 1.2, "google_news": 1.0, "trendspyg": 0.9, "hackernews": 0.85, "youtube_rss": 0.82,
          "telegram": 0.8, "reddit": 0.8, "google_suggest": 0.78, "google_trends": 0.7, "wikimedia": 0.7,
          "vk": 0.6}

# Telegram-каналы по категориям (публичные t.me/s — лучший RU-сигнал «что цепляет», без ключа)
TELEGRAM_CHANNELS = {
    "ai":            ["seeallochnaya", "data_secrets", "neuralshit", "ai_machinelearning_big_data"],
    "psychology":    ["psyhology_s", "psy_q"],
    "money":         ["tinkoff_invest_official", "investorI"],
    "history":       ["historyfeel", "back_to_history"],
    "business":      ["vcnews", "businesssecrets"],
    "personal_brand": ["smm_2_0", "internetanalytics"],
    "talking_objects": ["mudrostb"], "mystic": ["mistika_psy"],
    "soviet": ["back_in_ussr_official"], "whatif": ["naukatv"], "psy_story": ["psyhology_s"],
}


def _get(url: str, timeout: int = 20, headers: dict | None = None) -> bytes | None:
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    try:
        return urllib.request.urlopen(req, timeout=timeout).read()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
        return None


def _cat(niche: dict) -> dict:
    # category в схеме ниши = YouTube categoryId (int) → не годится как ключ NICHE_TRENDS;
    # строковую категорию принимаем, только если она реально есть в конфиге трендов
    c = niche.get("category")
    if not isinstance(c, str) or c not in NICHE_TRENDS:
        c = _NICHE_CATEGORY.get(niche.get("id", ""), "history")
    return NICHE_TRENDS.get(c, NICHE_TRENDS["history"])


def _lang(niche: dict) -> str:
    return "en" if niche.get("lang") == "en" else "ru"


# ──────────────────────────── источники ────────────────────────────

def google_news(query: str, lang: str = "ru", limit: int = 6) -> list[dict]:
    gl, hl, ceid = ("US", "en", "US:en") if lang == "en" else ("RU", "ru", "RU:ru")
    url = ("https://news.google.com/rss/search?" +
           urllib.parse.urlencode({"q": query, "hl": hl, "gl": gl, "ceid": ceid}))
    raw = _get(url)
    if not raw:
        return []
    out = []
    try:
        root = ET.fromstring(raw)
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            if title:
                out.append({"title": title, "source": "google_news", "url": item.findtext("link") or "",
                            "ts": item.findtext("pubDate") or "", "weight": WEIGHT["google_news"], "query": query})
            if len(out) >= limit:
                break
    except ET.ParseError:
        pass
    return out


def google_trends(geo: str = "RU", limit: int = 10) -> list[dict]:
    raw = _get(f"https://trends.google.com/trending/rss?geo={geo}")
    if not raw:
        return []
    out = []
    try:
        root = ET.fromstring(raw)
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            if title:
                out.append({"title": title, "source": "google_trends", "url": item.findtext("link") or "",
                            "ts": "", "weight": WEIGHT["google_trends"], "query": "trending"})
            if len(out) >= limit:
                break
    except ET.ParseError:
        pass
    return out


def _reddit_rss(subreddit: str, limit: int = 8, sort: str = "top") -> list[dict]:
    """Фолбэк для reddit: публичный Atom-фид .rss — работает там, где .json отдаёт
    403 Blocked (жёсткий IP-фильтр reddit на дата-центры/VPN). Апвотов в фиде нет →
    базовый вес источника, без буста."""
    q = "?t=day" if sort == "top" else ""
    raw = _get(f"https://www.reddit.com/r/{subreddit}/{sort}.rss{q}")
    if not raw:
        return []
    out = []
    try:
        root = ET.fromstring(raw)
        ns = {"a": "http://www.w3.org/2005/Atom"}
        for ent in root.findall("a:entry", ns):
            title = (ent.findtext("a:title", default="", namespaces=ns) or "").strip()
            link = ent.find("a:link", ns)
            if title:
                out.append({"title": title, "source": "reddit",
                            "url": (link.get("href") if link is not None else ""),
                            "ts": "", "weight": WEIGHT["reddit"], "query": subreddit})
            if len(out) >= limit:
                break
    except ET.ParseError:
        pass
    return out


def reddit_top(subreddit: str, limit: int = 8, sort: str = "top") -> list[dict]:
    suffix = "?t=day&limit=" + str(limit) if sort == "top" else "?limit=" + str(limit)
    raw = _get(f"https://www.reddit.com/r/{subreddit}/{sort}.json{suffix}")
    if not raw:
        return _reddit_rss(subreddit, limit, sort)   # .json заблокирован по IP → Atom-фид
    out = []
    try:
        for ch in json.loads(raw).get("data", {}).get("children", []):
            d = ch.get("data", {})
            title = (d.get("title") or "").strip()
            if title and not d.get("over_18"):
                ups = d.get("ups", 0)
                out.append({"title": title, "source": "reddit",
                            "url": "https://reddit.com" + d.get("permalink", ""),
                            "ts": "", "weight": WEIGHT["reddit"] * min(2.0, 1 + ups / 5000), "query": subreddit})
    except json.JSONDecodeError:
        pass
    return out


def hackernews(query: str, limit: int = 6) -> list[dict]:
    """HN Algolia (без ключа) — техно-сигнал для AI/личного бренда."""
    url = "https://hn.algolia.com/api/v1/search?" + urllib.parse.urlencode(
        {"query": query, "tags": "story", "numericFilters": "points>20"})
    raw = _get(url)
    if not raw:
        return []
    out = []
    try:
        for h in json.loads(raw).get("hits", [])[:limit]:
            title = (h.get("title") or "").strip()
            if title:
                out.append({"title": title, "source": "hackernews", "url": h.get("url") or "",
                            "ts": "", "weight": WEIGHT["hackernews"] * min(2.0, 1 + h.get("points", 0) / 500),
                            "query": query})
    except json.JSONDecodeError:
        pass
    return out


def vk_search(query: str, limit: int = 6) -> list[dict]:
    tok = core.secret("VK_ACC1_TOKEN", required=False) or core.secret("VK_TOKEN", required=False)
    if not tok:
        return []
    # токен — в POST-body, НЕ в query string (иначе течёт в логи/прокси)
    try:
        import requests
        raw = requests.post("https://api.vk.com/method/newsfeed.search",
                            data={"q": query, "count": limit, "access_token": tok, "v": "5.199"},
                            timeout=20).content
    except Exception:  # noqa: BLE001
        return []
    if not raw:
        return []
    out = []
    try:
        for it in json.loads(raw).get("response", {}).get("items", []):
            txt = (it.get("text") or "").strip().split("\n")[0][:160]
            if len(txt) > 25:
                out.append({"title": txt, "source": "vk", "url": "", "ts": "",
                            "weight": WEIGHT["vk"] * min(2.0, 1 + it.get("likes", {}).get("count", 0) / 1000),
                            "query": query})
    except json.JSONDecodeError:
        pass
    return out


# ──────────────────────────── сбор по нише ────────────────────────────

_TG_MSG = re.compile(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', re.DOTALL)
_TAGS = re.compile(r"<[^>]+>")


def telegram_channel(channel: str, limit: int = 6) -> list[dict]:
    """Свежие посты публичного канала через t.me/s/<channel> (HTML, без ключа/авторизации)."""
    raw = _get(f"https://t.me/s/{channel}", headers={"User-Agent": UA})
    if not raw:
        return []
    html = raw.decode("utf-8", "replace")
    out = []
    for m in _TG_MSG.findall(html)[-limit * 2:]:          # берём последние сообщения
        text = _TAGS.sub(" ", m.replace("<br/>", " ").replace("<br>", " "))
        text = re.sub(r"\s+", " ", text).strip()
        if 20 <= len(text) <= 220 and "t.me" not in text.lower():
            out.append({"title": text, "source": "telegram", "url": f"https://t.me/{channel}",
                        "ts": "", "weight": WEIGHT["telegram"], "query": channel})
        if len(out) >= limit:
            break
    return out


_YT_TITLE = re.compile(r"<title>(.*?)</title>", re.DOTALL)


def youtube_rss(channel_ids, limit: int = 10) -> list[dict]:
    """Свежие заголовки видео конкурентов через публичный YouTube RSS (без ключа/авторизации).
    Для каждого channel_id GET https://www.youtube.com/feeds/videos.xml?channel_id=<ID> (Atom XML).
    Заголовки последних видео = сигнал «что сейчас залетает в нише». Только метаданные, видео не качаем.
    Любой сбой (сеть/парс) по каналу → пропускаем его; пустой список каналов → []."""
    if not channel_ids:
        return []
    out: list[dict] = []
    for cid in channel_ids:
        cid = (cid or "").strip()
        if not cid:
            continue
        try:
            raw = _get(f"https://www.youtube.com/feeds/videos.xml?channel_id={urllib.parse.quote(cid)}",
                       headers={"User-Agent": UA})
            if not raw:
                continue
            titles: list[str] = []
            try:                                  # 1) надёжно через xml.etree (Atom)
                root = ET.fromstring(raw)
                ns = {"a": "http://www.w3.org/2005/Atom"}
                for ent in root.findall("a:entry", ns):
                    t = (ent.findtext("a:title", default="", namespaces=ns) or "").strip()
                    if t:
                        titles.append(t)
            except ET.ParseError:                 # 2) фолбэк — регуляркой по <title>
                titles = [re.sub(r"\s+", " ", t).strip()
                          for t in _YT_TITLE.findall(raw.decode("utf-8", "replace"))]
                titles = titles[1:] if titles else []   # первый <title> = имя канала, не видео
            for t in titles[:limit]:
                t = core.sanitize_external(t)            # анти-инъекция перед использованием как тема
                if len(t) >= 12:
                    out.append({"title": t, "source": "youtube_rss",
                                "url": f"https://www.youtube.com/channel/{cid}",
                                "ts": "", "weight": WEIGHT["youtube_rss"], "query": cid})
        except Exception as e:  # noqa: BLE001 — источник опционален, не роняем сбор
            core.log_error("youtube_rss", e, channel=cid)
            continue
    return out


_SUGGEST_CACHE: dict[tuple, list] = {}


def google_suggest(seed: str, hl: str = "ru", limit: int = 8) -> list[dict]:
    """Google Autocomplete (suggestqueries) — реальные формулировки аудитории «языком людей».
    hl=ru → русские длиннохвостые запросы (готовые хуки/темы Q&A). Без ключа. С внутри-сессионным кэшем."""
    seed = (seed or "").strip()
    if not seed:
        return []
    ck = (seed, hl)
    if ck in _SUGGEST_CACHE:
        return _SUGGEST_CACHE[ck]
    # ВАЖНО: https (не http), client=firefox отдаёт чистый JSON [запрос, [подсказки...]]
    url = "https://suggestqueries.google.com/complete/search?" + urllib.parse.urlencode(
        {"client": "firefox", "hl": hl, "q": seed})
    raw = _get(url, timeout=15)
    out = []
    if raw:
        try:
            d = json.loads(raw.decode("utf-8", "replace"))
            for s in (d[1] if len(d) > 1 else [])[:limit]:
                s = (s or "").strip()
                if len(s) >= 12:
                    out.append({"title": s, "source": "google_suggest", "url": "", "ts": "",
                                "weight": WEIGHT["google_suggest"], "query": seed})
        except (json.JSONDecodeError, IndexError, TypeError):
            pass
    _SUGGEST_CACHE[ck] = out
    return out


def wiki_top(project: str = "en.wikipedia", limit: int = 10, day_lag: int = 2) -> list[dict]:
    """Wikimedia Pageviews — топ читаемых статей вики-проекта (ранний сигнал «что читают сейчас»).
    project параметризован: en.wikipedia для EN-ниш, ru.wikipedia для RU.
    Официальный REST, без ключа. Берём вчерашний/позавчерашний день (есть задержка данных)."""
    d = core._now().date() - dt.timedelta(days=day_lag)
    url = (f"https://wikimedia.org/api/rest_v1/metrics/pageviews/top/{project}/all-access/"
           f"{d.year}/{d.month:02d}/{d.day:02d}")
    raw = _get(url, timeout=20)
    if not raw:
        return []
    out = []
    try:
        arts = json.loads(raw.decode("utf-8", "replace"))["items"][0]["articles"]
        for a in arts:
            name = a.get("article", "")
            if ":" in name or name in ("Заглавная_страница", "Main_Page"):   # служебные/спец-страницы — мимо
                continue
            title = name.replace("_", " ").strip()
            if len(title) >= 4:
                out.append({"title": title, "source": "wikimedia",
                            "url": f"https://{project.split('.')[0]}.wikipedia.org/wiki/{name}",
                            "ts": "", "weight": WEIGHT["wikimedia"], "query": "pageviews"})
            if len(out) >= limit:
                break
    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
        pass
    return out


def trendspyg_signals(geo: str = "RU", limit: int = 10) -> list[dict]:
    """Живой Google Trends через trendspyg (RSS, кэш) — замена заброшенного pytrends, без ключа/браузера.
    Отдаёт страновые тренды + по 1 связанной новости (часто конкретнее и «хукабельнее»)."""
    try:
        import trendspyg
        res = trendspyg.download_google_trends_rss(geo=geo, output_format="dict",
                                                   include_articles=True, max_articles_per_trend=1, cache=True)
    except Exception as e:  # noqa: BLE001 — источник опционален, не роняем сбор
        core.log_error("trendspyg", e)
        return []
    out = []
    for it in (res or [])[:limit]:
        if not isinstance(it, dict):
            continue
        title = (it.get("trend") or it.get("title") or "").strip()
        if len(title) >= 4:
            tmin = it.get("traffic_min") or 0
            out.append({"title": title, "source": "trendspyg", "url": it.get("explore_link", ""),
                        "ts": "", "weight": round(WEIGHT["trendspyg"] * min(1.6, 1 + tmin / 2000), 2),
                        "query": "trending"})
        for art in (it.get("news_articles") or [])[:1]:
            at = (art.get("title") if isinstance(art, dict) else "") or ""
            at = at.strip()
            if len(at) >= 12:
                out.append({"title": at, "source": "trendspyg",
                            "url": (art.get("url") if isinstance(art, dict) else "") or "",
                            "ts": "", "weight": round(WEIGHT["trendspyg"] * 0.9, 2), "query": "trending_news"})
    return out


_TMCP_COOLDOWN: dict[str, float] = {}   # ключ → unix-ts, до которого пропускаем (исчерпана квота)


def _trends_series(keyword: str, source: str = "google search") -> list[float]:
    """Ряд интереса к ключевику во времени (Trends MCP REST) с РОТАЦИЕЙ ключей.
    Кончилась квота у одного ключа → пробуем следующий. [] при сбое/нет данных/нет ключей."""
    keys = [k.strip() for k in (core.secret("TRENDS_MCP_API_KEY", required=False) or "").split(",") if k.strip()]
    if not keys:
        return []
    now = time.time()
    data = None
    for key in keys:
        if _TMCP_COOLDOWN.get(key, 0) > now:
            continue
        data = core.http_json(
            "https://api.trendsmcp.ai/api",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            data=json.dumps({"source": source, "keyword": keyword}).encode(),
            timeout=30, retries=1,
        )
        if data is not None:               # ключ ответил (даже 404 no_data) → рабочий, выходим
            break
        _TMCP_COOLDOWN[key] = now + 3600    # None = сеть/квота/429 → ключ на час в карантин
    if not data:
        return []
    body = data.get("body")
    try:
        arr = json.loads(body) if isinstance(body, str) else (body or [])
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(arr, list):
        return []
    return [float(p["value"]) for p in arr if isinstance(p, dict) and p.get("value") is not None]


def trend_momentum(keyword: str, source: str = "google search") -> float:
    """Импульс темы: средний интерес за последние ~2 мес / за предыдущие ~4 мес.
    >1 = тема растёт, <1 = угасает. 1.0 если данных мало. Для ранжирования/буста тем."""
    s = _trends_series(keyword, source)
    if len(s) < 16:
        return 1.0
    recent = sum(s[-8:]) / 8
    base = sum(s[-26:-8]) / max(1, len(s[-26:-8]))
    if base <= 0:
        return 1.0
    return round(min(2.0, max(0.4, recent / base)), 2)


def rising_topics(niche: dict, limit: int = 3, max_calls: int = 2) -> list[dict]:
    """Какие КЛЮЧЕВИКИ ниши набирают обороты сейчас (Trends MCP). Возвращает их как кандидаты
    с весом, усиленным импульсом → selector/LLM лепит свежую тему именно под растущий тренд.
    Экономим квоту (free 100/мес): не более max_calls вызовов за раз."""
    if not core.secret("TRENDS_MCP_API_KEY", required=False):
        return []
    cfg = _cat(niche)
    kws = cfg.get(_lang(niche), cfg.get("ru", []))[:max_calls]
    out = []
    for kw in kws:
        m = trend_momentum(kw)
        if m >= 1.08:                       # тема ощутимо растёт — добавляем как сигнал
            out.append({"title": f"{kw} (растёт)", "source": "trends_mcp", "url": "", "ts": "",
                        "weight": round(WEIGHT.get("trends_mcp", 1.2) * m, 2), "query": kw, "momentum": m})
    return sorted(out, key=lambda x: -x["momentum"])[:limit]


def gather(niche: dict, per_source: int = 6) -> list[dict]:
    """Собрать кандидатов-тренды по нише из всех источников."""
    lang = _lang(niche)
    cfg = _cat(niche)
    queries = cfg.get(lang, cfg.get("ru", []))
    cands: list[dict] = []

    # Google News по 2-3 ключевикам ниши
    for q in queries[:3]:
        cands += google_news(q, lang=lang, limit=per_source)
    # Reddit top + rising (rising ловит набирающее)
    subs = cfg.get("subreddits", [])[:3]
    for sub in subs:
        cands += reddit_top(sub, limit=per_source)
    if subs:
        cands += reddit_top(subs[0], limit=per_source, sort="rising")
    # HackerNews — техно-сигнал для AI/личного бренда
    cat = niche.get("category") or _NICHE_CATEGORY.get(niche.get("id", ""))
    if cat in ("ai", "personal_brand"):
        for q in cfg.get("en", [])[:2]:
            cands += hackernews(q)
    # Страновой тренд «что в воздухе»: trendspyg (живой Google Trends + новости, кэш) с фолбэком на RSS
    geo = "US" if lang == "en" else "RU"
    cands += trendspyg_signals(geo, limit=10) or google_trends(geo, limit=8)
    # Google Autocomplete — реальные формулировки аудитории «языком людей» (хуки/Q&A) по 2 ключевикам ниши
    for q in queries[:2]:
        cands += google_suggest(q, hl=("en" if lang == "en" else "ru"), limit=8)
    # Wikimedia Pageviews — что читают прямо сейчас (проект по языку ниши: en/ru)
    cands += wiki_top("en.wikipedia" if lang == "en" else "ru.wikipedia", limit=10)
    # Trends MCP — какие ключевики ниши РАСТУТ сейчас (импульс), бустим их как кандидаты (если есть ключ)
    cands += rising_topics(niche, limit=3, max_calls=2)
    # YouTube RSS конкурентов ниши (публичный, без ключа) — свежие заголовки = сигнал трендов.
    # Каналы берём из опционального поля niche["rss_channels"]; нет/пусто → no-op (готовая инфраструктура).
    cands += youtube_rss(niche.get("rss_channels") or [], limit=per_source)
    # Telegram-каналы ниши (t.me/s, без ключа — сильный RU-сигнал «что сейчас цепляет»)
    if lang == "ru":
        for ch in TELEGRAM_CHANNELS.get(cat, [])[:3]:
            cands += telegram_channel(ch, limit=per_source)
    # VK по ключевику (если токен есть)
    if lang == "ru":
        for q in queries[:1]:
            cands += vk_search(q, limit=per_source)

    # лёгкая чистка/дедуп по заголовку
    seen, out = set(), []
    for c in cands:
        key = c["title"].lower()[:80]
        if key in seen or len(c["title"]) < 12:
            continue
        seen.add(key)
        out.append(c)
    return out


if __name__ == "__main__":
    core.load_local_secrets()
    nid = sys.argv[1] if len(sys.argv) > 1 else "history_docs"
    res = gather(core.get_niche(nid))
    print(f"[{nid}] кандидатов: {len(res)}")
    for c in sorted(res, key=lambda x: -x["weight"])[:15]:
        print(f"  {c['weight']:.2f} [{c['source']:12}] {c['title'][:80]}")
