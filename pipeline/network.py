"""Сеть автоканалов: тема дня из YouTube → свой угол → сборка → проверка схожести → TG владельцу.

Каждый канал сети = ниша(и) в niches.json с "network": "<канал>". У канала свои голос, стиль
картинок, субтитры, структура и время выхода — каналы не должны выглядеть копиями друг друга.

  python3 -m pipeline.network run <niche_id>     # одна сборка (шортс или длинный — по нише)
  python3 -m pipeline.network pick <niche_id>    # только выбор темы (без сборки), для отладки
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import core  # noqa: E402


# правило безопасности темы; ниша может задать своё (safety_rule) — исторической нише общий запрет
# «no politics, no real tragedies» отсекал бы вообще все темы
SAFETY_DEFAULT = ("no medical/financial advice, no politics, no real tragedies or crimes with victims, "
                  "no children")


def pick(niche: dict, exclude: set | None = None, cands: list | None = None) -> dict | None:
    """Выбрать ролик-источник и придумать СВОЙ угол. None → нет трендов (сборка пойдёт по
    обычному селектору ниши, без источника). exclude — video_id источников, уже забракованных."""
    from pipeline import llm, topics_db, yt_trends
    if cands is None:                         # поиск YouTube дорогой (100 ед.) и ловит 429 — один раз за ран
        cands = yt_trends.discover(niche)
    cands = [c for c in cands if c["video_id"] not in (exclude or set())]
    if not cands:
        return None
    topics_db.init()
    done = topics_db.recent_titles(niche=niche["id"], days=60) or []
    lines = [f"{i}. {c['title']} — {c['views']} views in {c['age_h']}h (channel subs {c['subs']})"
             for i, c in enumerate(cands[:10])]
    system = (
        "You are the head producer of a faceless English YouTube channel.\n"
        f"CHANNEL: {niche.get('title')} — {niche.get('topic_brief')}\n"
        f"FORMAT: {'YouTube Short, 35-50 seconds' if niche.get('format') == 'short' else 'long video, 8-10 minutes'}\n"
        "From the list of videos trending in the last 48 hours pick ONE whose underlying SUBJECT fits the "
        f"channel and can be retold with a fresh angle, and that is SAFE ({niche.get('safety_rule') or SAFETY_DEFAULT}). "
        "Then invent OUR OWN video: a new "
        "title (must not reuse the source title's wording), a different angle and a different structure. "
        "We only borrow the subject; overlap with the source must stay under 15%.\n"
        'Return JSON {"pick": <index>, "topic": "<our title>", "angle": "<2 sentences: our angle and '
        'structure, how it differs from the source>"}. If nothing fits, return {"pick": -1}.')
    user = "TRENDING NOW:\n" + "\n".join(lines)
    if done:
        user += "\n\nALREADY MADE (do not repeat):\n" + "\n".join(f"- {t}" for t in done[:40])
    from pipeline.originality import loads
    d = None
    for _ in range(3):   # reasoning-модели каскада иногда обрывают JSON — повтор
        try:
            d = loads(llm.chat(system, user, json_mode=True, max_tokens=3000, temp=0.7))
            break
        except Exception as e:  # noqa: BLE001
            core.log_error("network.pick", e, niche=niche["id"])
    if not d:
        return None
    i = int(d.get("pick", -1))
    if not (0 <= i < min(10, len(cands))) or not d.get("topic"):
        return None
    src = cands[i]
    src["transcript"] = yt_trends.transcript(src["video_id"])
    return {"topic": core.sanitize_external(d["topic"]), "angle": core.sanitize_external(d.get("angle", "")),
            "source": src}


def run(niche_id: str, max_sources: int = 3) -> list:
    """Источник не прошёл проверку схожести (дважды > LIMIT) → берём следующий тренд, до
    max_sources штук; все забракованы → тема из обычного селектора ниши, без источника."""
    from pipeline import autopilot
    core.load_local_secrets()
    niche = core.get_niche(niche_id)
    from pipeline import yt_trends
    tried: set = set()
    cands = yt_trends.discover(niche)
    for _ in range(max_sources):
        p = pick(niche, exclude=tried, cands=cands)
        if not p:
            break
        src = p["source"]
        tried.add(src["video_id"])
        core.NICHE_OVERRIDES[niche_id] = {
            "_source": {k: src.get(k, "") for k in ("title", "description", "transcript")},
            "_source_url": f"https://youtu.be/{src['video_id']}",
            "topic_brief": (niche.get("topic_brief", "") + f"\n\nTODAY'S VIDEO: {p['topic']}. ANGLE: {p['angle']} "
                            "Write it from your own knowledge with your own structure and examples."),
        }
        core.log("network: тема дня", niche=niche_id, topic=p["topic"], source=src["title"],
                 views=src["views"], velocity=src["velocity"])
        try:
            return autopilot.post_youtube(niche_id, topic=p["topic"])
        except RuntimeError as e:
            if "Схожесть с источником" not in str(e):
                raise
            core.log("network: источник забракован по схожести — следующий тренд", level="warn",
                     niche=niche_id, source=src["title"])
    core.NICHE_OVERRIDES.pop(niche_id, None)
    core.log("network: трендов нет/все забракованы — тема из селектора ниши", level="warn", niche=niche_id)
    return autopilot.post_youtube(niche_id, topic=None)


if __name__ == "__main__":
    core.load_local_secrets()
    cmd, nid = sys.argv[1], sys.argv[2]
    if cmd == "pick":
        r = pick(core.get_niche(nid))
        print(json.dumps({k: v for k, v in (r or {}).items() if k != "source"} | (
            {"source": {k: r["source"][k] for k in ("title", "views", "velocity", "video_id")}} if r else {}),
            ensure_ascii=False, indent=2))
    else:
        print(run(nid))
