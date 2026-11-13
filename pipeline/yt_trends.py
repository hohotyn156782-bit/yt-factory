"""Тема дня из YouTube: свежие (24-48 ч) ролики ниши, которые растут быстрее остальных.

Источник — YouTube Data API v3 (ключ YOUTUBE_API_KEY, бесплатная квота 10 000 ед./сутки):
search.list = 100 ед., videos.list / channels.list = 1 ед. На один канал сети в день уходит
~3 поиска = ~300 ед., т.е. 5 каналов × (шортс + длинный) укладываются с запасом.

Скоринг: скорость просмотров (views/час с момента публикации) × «аутлайер-фактор»
(views / подписчики канала, зажат 0.5..20). Аутлайер у маленького канала = тема, а не бренд.
Ролик-источник НЕ копируется: дальше берётся только идея (см. network.py + originality.py).
"""
import datetime as dt
import json
import math
import os
import pathlib
import re
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import core  # noqa: E402

API = "https://www.googleapis.com/youtube/v3/"
_ISO_DUR = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


def _key() -> str:
    return os.environ.get("YOUTUBE_API_KEY", "").split(",")[0].strip()


def _get(method: str, **params) -> dict:
    params["key"] = _key()
    url = API + method + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read())


def _dur_s(iso: str) -> int:
    m = _ISO_DUR.fullmatch(iso or "")
    if not m:
        return 0
    h, mi, s = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mi * 60 + s


def _is_english(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 8:
        return False
    return sum(1 for c in letters if c.isascii()) / len(letters) > 0.9


def discover(niche: dict, hours: int = 48, per_query: int = 15) -> list[dict]:
    """Кандидаты «тема дня» для ниши, лучшие первыми. [] при отсутствии ключа/сбое API."""
    if not _key():
        core.log("yt_trends: нет YOUTUBE_API_KEY — пропуск", level="warn")
        return []
    queries = niche.get("trend_queries") or []
    if not queries:
        return []
    fmt = niche.get("format", "short")
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    exclude = set(niche.get("trend_exclude_channels") or [])
    ids: dict[str, str] = {}
    for q in queries[:3]:
        try:
            res = _get("search", part="snippet", type="video", order="viewCount", q=q,
                       publishedAfter=since, regionCode="US", relevanceLanguage="en",
                       videoDuration=("short" if fmt == "short" else "medium"), maxResults=per_query)
        except Exception as e:  # noqa: BLE001
            core.log_error("yt_trends.search", e, q=q)
            continue
        for it in res.get("items", []):
            vid = (it.get("id") or {}).get("videoId")
            if vid and it["snippet"].get("channelId") not in exclude:
                ids.setdefault(vid, q)
    if not ids:
        return []
    try:
        vids = _get("videos", part="snippet,statistics,contentDetails", id=",".join(list(ids)[:50]))["items"]
        ch_ids = sorted({v["snippet"]["channelId"] for v in vids})
        chans = _get("channels", part="statistics", id=",".join(ch_ids[:50]))["items"]
    except Exception as e:  # noqa: BLE001
        core.log_error("yt_trends.details", e)
        return []
    subs = {c["id"]: int(c["statistics"].get("subscriberCount") or 0) for c in chans}
    now = dt.datetime.now(dt.timezone.utc)
    out = []
    for v in vids:
        sn, st = v["snippet"], v.get("statistics", {})
        title = sn.get("title", "")
        if not _is_english(title):
            continue
        dur = _dur_s(v.get("contentDetails", {}).get("duration", ""))
        if fmt == "short" and not (8 <= dur <= 180):
            continue
        if fmt == "long" and dur < 240:
            continue
        views = int(st.get("viewCount") or 0)
        pub = dt.datetime.fromisoformat(sn["publishedAt"].replace("Z", "+00:00"))
        age_h = max(1.0, (now - pub).total_seconds() / 3600)
        velocity = views / age_h
        ratio = views / max(1, subs.get(sn["channelId"], 0))
        outlier = min(20.0, max(0.5, ratio))
        out.append({
            "video_id": v["id"], "title": title, "channel": sn.get("channelTitle", ""),
            "channel_id": sn["channelId"], "views": views, "age_h": round(age_h, 1),
            "velocity": round(velocity, 1), "subs": subs.get(sn["channelId"], 0),
            "score": round(velocity * math.sqrt(outlier), 1), "query": ids[v["id"]],
            "description": (sn.get("description") or "")[:1500],
            "tags": (sn.get("tags") or [])[:20], "duration": dur,
        })
    out.sort(key=lambda x: -x["score"])
    return out


def transcript(video_id: str) -> str:
    """Субтитры ролика-источника (для проверки схожести). На CI YouTube режет IP GitHub —
    тогда пусто, и сверка идёт по названию+описанию. Отключается CF_NO_YTDLP=1."""
    if os.environ.get("CF_NO_YTDLP") == "1":
        return ""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi  # type: ignore
        api = YouTubeTranscriptApi()
        fetched = api.fetch(video_id, languages=["en"])
        return " ".join(s.text for s in fetched)[:20000]
    except Exception:  # noqa: BLE001 — нет субтитров/блок/нет пакета: сверка по метаданным
        return ""


if __name__ == "__main__":
    core.load_local_secrets()
    nid = sys.argv[1] if len(sys.argv) > 1 else None
    n = core.get_niche(nid) if nid else {"format": "short", "trend_queries": ["space facts", "universe explained"]}
    for c in discover(n)[:10]:
        print(f"{c['score']:>10} v/h={c['velocity']:<8} subs={c['subs']:<8} {c['title'][:80]}")
