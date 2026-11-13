"""Проверка, что наш ролик не копия ролика-источника (порог владельца: схожесть ≤ 20%).

Три независимых измерения, итог = максимум:
  • lexical   — доля наших словесных 4-грамм, встречающихся в тексте источника (копипаст фраз);
  • title     — пересечение значимых слов заголовков (Jaccard);
  • structure — оценка LLM-судьи 0-100, насколько совпадают угол подачи, порядок пунктов и
                ключевые примеры (перефраз той же структуры — тоже копия).
Судья — бесплатный LLM-каскад (pipeline.llm). Нет ответа судьи → structure не учитывается,
решают lexical/title (fail-open только для судьи, не для всей проверки).
"""
import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import core  # noqa: E402

LIMIT = 20
_STOP = set("""a an the of to in on at for and or but is are was were be been it its this that these
those you your we our they their i me my he she his her with from by as about into than then so if
what why how when who which can could would should will just not no do does did have has had
there here more most very all any some one two""".split())


def _words(t: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", (t or "").lower())


def _ngrams(ws: list[str], n: int = 4) -> set:
    return {tuple(ws[i:i + n]) for i in range(len(ws) - n + 1)}


def lexical(ours: str, source: str) -> float:
    a, b = _ngrams(_words(ours)), _ngrams(_words(source))
    if not a or not b:
        return 0.0
    return 100.0 * len(a & b) / len(a)


def title_sim(ours: str, source: str) -> float:
    a = {w for w in _words(ours) if w not in _STOP}
    b = {w for w in _words(source) if w not in _STOP}
    if not a or not b:
        return 0.0
    return 100.0 * len(a & b) / len(a | b)


def loads(txt: str) -> dict:
    """JSON из ответа LLM: часть провайдеров оборачивает его в ```json или пишет текст вокруг."""
    m = re.search(r"\{.*\}", txt or "", re.S)
    if not m:
        raise ValueError(f"нет JSON в ответе: {(txt or '')[:80]!r}")
    return json.loads(m.group(0))


def structure(ours: str, source: str) -> float | None:
    from pipeline import llm
    system = ("You are a strict YouTube originality auditor. Compare OUR script with the SOURCE video "
              "material. Estimate 0-100 how much OUR script reuses the SOURCE's angle, sequence of points, "
              "specific examples, jokes and phrasing. Shared general topic alone is ~5-10. "
              'Return JSON {"similarity": <int>, "reason": "<short>"}')
    user = f"SOURCE:\n{source[:6000]}\n\nOUR SCRIPT:\n{ours[:4000]}"
    for _ in range(2):   # 2 попытки: каскад сам сменит провайдера при сбое
        try:
            d = loads(llm.chat(system, user, json_mode=True, max_tokens=1000, temp=0.1))
            return float(d.get("similarity"))
        except Exception as e:  # noqa: BLE001
            core.log_error("originality.structure", e)
    return None


def score(our_title: str, our_text: str, src: dict) -> dict:
    """src: {'title','description','transcript'}. Возвращает {ok, similarity, parts}."""
    src_text = " ".join([src.get("title", ""), src.get("description", ""), src.get("transcript", "")])
    parts = {"lexical": round(lexical(our_text, src_text), 1),
             "title": round(title_sim(our_title, src.get("title", "")), 1)}
    st = structure(f"{our_title}\n{our_text}", src_text)
    if st is not None:
        parts["structure"] = round(st, 1)
    sim = max(parts.values())
    return {"ok": sim <= LIMIT, "similarity": sim, "parts": parts, "limit": LIMIT}


def script_text(sc: dict) -> str:
    segs = " ".join(s.get("text", "") for s in sc.get("segments", []))
    chapters = " ".join(c.get("text", "") for c in sc.get("chapters", []) if isinstance(c, dict))
    return " ".join([sc.get("hook", ""), segs, chapters, sc.get("outro", "")]).strip()
