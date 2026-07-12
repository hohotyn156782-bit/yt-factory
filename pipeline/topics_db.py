"""База данных выпущенных роликов + умная защита от повторов тем.

SQLite на диске данных (DATA_ROOT/factory.db). Хранит каждый выпущенный ролик:
ниша, тема, «отпечаток» темы (нормализованные основы слов), хук, путь, дата, вирусность.

Защита от дублей — НЕ по точной строке, а по смыслу:
  • нормализация: нижний регистр + транслит + выкидываем стоп-слова + грубый стемминг (основы);
  • сравнение через коэффициент Жаккара по множествам основ + проверку «вложенности»;
  • КРОСС-НИШЕВО (тема «как работает ChatGPT» не повторится ни в одной нише) и устойчиво
    к вариациям («ChatGPT 4» ≈ «ChatGPT-4» ≈ «чат gpt»).

API: init() · reserve_topic(...) · commit_topic(...) · release_topic(...) · record(...) ·
     is_duplicate(topic) · recent_titles(days, niche) · stats()
Новый путь (атомарный анти-дубль): reserve_topic в script → commit_topic при успехе / release_topic при браке.
Старые record/is_duplicate сохранены для совместимости.
"""
import re
import sqlite3

import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import core  # noqa: E402

DB = core.DATA_ROOT / "factory.db"

# стоп-слова (служебные — не несут смысла темы), ru + en
_STOP = {
    "как", "что", "почему", "это", "так", "вот", "там", "тут", "для", "под", "над", "при", "про",
    "the", "a", "an", "of", "to", "in", "on", "for", "with", "and", "or", "is", "are", "your", "you",
    "why", "how", "what", "this", "that", "его", "её", "их", "они", "она", "оно", "был", "была",
    "если", "бы", "же", "ли", "не", "ни", "из", "до", "от", "за", "по", "на", "в", "и", "с",
}
_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z",
    "и": "i", "й": "i", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sh",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "u", "я": "a",
}


def _conn() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False + WAL — панель собирает ролики в фоновых потоках, БД пишется
    # из нескольких потоков параллельно → без этого возможна порча файла.
    c = sqlite3.connect(str(DB), check_same_thread=False, timeout=10)
    c.row_factory = sqlite3.Row
    try:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=5000")
    except sqlite3.OperationalError:
        pass
    return c


def init() -> None:
    c = _conn()
    c.execute("""CREATE TABLE IF NOT EXISTS topics(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        niche TEXT, topic TEXT, fingerprint TEXT, lang TEXT DEFAULT 'ru',
        dir TEXT DEFAULT '', hook TEXT DEFAULT '', virality INTEGER DEFAULT 0,
        created TEXT)""")
    # анти-дубль-контент: какие сток-клипы уже использованы в нише (не повторять между роликами)
    c.execute("""CREATE TABLE IF NOT EXISTS used_media(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        niche TEXT, media_key TEXT, created TEXT)""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_used_media ON used_media(niche, media_key)")
    # миграция: статус резерва темы (атомарный анти-дубль при параллельных сборках)
    for stmt in ("ALTER TABLE topics ADD COLUMN status TEXT DEFAULT 'built'",
                 "ALTER TABLE topics ADD COLUMN reserved_at TEXT"):
        try:
            c.execute(stmt)
        except sqlite3.OperationalError:
            pass                       # колонка уже есть
    c.execute("CREATE INDEX IF NOT EXISTS idx_topics_created ON topics(created)")
    c.commit()
    # чистим протухшие резервы (брошенные сборки старше суток), чтобы они не блокировали темы
    stale = (core._now() - __import__("datetime").timedelta(days=1)).isoformat()
    try:
        c.execute("DELETE FROM topics WHERE status='reserved' AND reserved_at < ?", (stale,))
        c.commit()
    except sqlite3.OperationalError:
        pass
    # одноразовый импорт прошлых тем из history.jsonl (чтобы не повторять уже сделанное)
    n = c.execute("SELECT COUNT(*) FROM topics").fetchone()[0]
    if n == 0:
        seeded = 0
        for e in core.load_history():
            t = e.get("topic")
            if t and e.get("status") == "built":
                c.execute("INSERT INTO topics(niche,topic,fingerprint,lang,dir,created) VALUES(?,?,?,?,?,?)",
                          (e.get("niche", ""), t, " ".join(sorted(_fingerprint(t))),
                           e.get("lang", "ru"), e.get("dir", ""), e.get("ts", "")))
            seeded += 1
        if seeded:
            c.commit()
            core.log(f"topics_db: импортировано из истории {seeded} записей", level="info")
    c.close()


def _stem(word: str) -> str:
    """Грубая основа: транслит + срез частых русских окончаний → одна форма для падежей."""
    w = "".join(_TRANSLIT.get(ch, ch) for ch in word.lower())
    w = re.sub(r"[^a-z0-9]", "", w)
    for suf in ("ami", "yami", "ovi", "ami", "ogo", "ego", "ymi", "imi", "yh", "ih", "om", "em",
                "ov", "ev", "yu", "ya", "oi", "ei", "ie", "ye", " y", "a", "o", "e", "i", "u", "y"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def _fingerprint(text: str) -> set:
    """Множество значимых основ слов темы (для сравнения по смыслу)."""
    words = re.findall(r"\w+", (text or "").lower(), flags=re.UNICODE)
    out = set()
    for w in words:
        if w in _STOP or len(w) < 3:
            continue
        s = _stem(w)
        if len(s) >= 3:
            out.add(s)
    return out


def _similar(a: set, b: set) -> float:
    """Сходство тем: Жаккар, но с бонусом за вложенность (короткая тема ⊂ длинной = дубль)."""
    if not a or not b:
        return 0.0
    inter = len(a & b)
    jac = inter / len(a | b)
    subset = inter / min(len(a), len(b))     # одна тема почти целиком внутри другой
    return max(jac, subset * 0.9)


def _match_rows(fp: set, rows, threshold: float) -> str:
    """Найти среди строк (topic, fingerprint) первую, чей отпечаток похож на fp (Жаккар+вложенность).
    Единая логика сходства для is_duplicate и reserve_topic — не дублировать/не упрощать."""
    for r in rows:
        other = set((r["fingerprint"] or "").split())
        if _similar(fp, other) >= threshold:
            return r["topic"]
    return ""


def is_duplicate(topic: str, days: int = 60, threshold: float = 0.6) -> tuple[bool, str]:
    """Похожа ли тема на уже выпущенную за `days` дней (кросс-нишево). → (дубль?, на что похоже)."""
    fp = _fingerprint(topic)
    if not fp:
        return False, ""
    cutoff = (core._now() - __import__("datetime").timedelta(days=days)).isoformat()
    c = _conn()
    try:
        rows = c.execute("SELECT topic, fingerprint FROM topics WHERE created >= ?", (cutoff,)).fetchall()
    except sqlite3.OperationalError:
        return False, ""
    finally:
        c.close()
    match = _match_rows(fp, rows, threshold)
    return (bool(match), match)


def record(niche: str, topic: str, lang: str = "ru", dir: str = "",
           hook: str = "", virality: int = 0) -> None:
    if not topic:
        return
    c = _conn()
    c.execute("INSERT INTO topics(niche,topic,fingerprint,lang,dir,hook,virality,created) "
              "VALUES(?,?,?,?,?,?,?,?)",
              (niche, topic, " ".join(sorted(_fingerprint(topic))), lang, dir, hook,
               int(virality or 0), core._now().isoformat()))
    c.commit()
    c.close()


def reserve_topic(niche: str, topic: str, lang: str = "ru", days: int = 60,
                  threshold: float = 0.6) -> tuple[bool, str]:
    """Атомарно зарезервировать тему: проверка дубля + INSERT(status='reserved') в ОДНОЙ транзакции.
    Закрывает окно гонки между script.is_duplicate и build.record при параллельных сборках.
    → (True, "") если тема свободна и зарезервирована; (False, match) если дубль уже есть/резервируется.

    Использует ту же логику сходства, что и is_duplicate (_fingerprint/_similar/_match_rows) — окно
    учитывает и status='built', и status='reserved'. Конкуренты сериализуются через BEGIN IMMEDIATE
    + busy_timeout=5000 (если двое берут одну тему одновременно — второй увидит чужой 'reserved')."""
    if not topic:
        return False, ""
    fp = _fingerprint(topic)
    if not fp:                                  # пустой отпечаток — резервировать нечего/нечем сравнить
        return False, ""
    now = core._now().isoformat()
    cutoff = (core._now() - __import__("datetime").timedelta(days=days)).isoformat()
    fp_str = " ".join(sorted(fp))
    # отдельное соединение в ручном режиме транзакций (isolation_level=None) под BEGIN IMMEDIATE
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(DB), check_same_thread=False, timeout=10, isolation_level=None)
    c.row_factory = sqlite3.Row
    try:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=5000")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("BEGIN IMMEDIATE")            # эксклюзивная блокировка записи на время проверки+вставки
        rows = c.execute("SELECT topic, fingerprint FROM topics WHERE created >= ?", (cutoff,)).fetchall()
        match = _match_rows(fp, rows, threshold)   # та же нормализация/сходство, что и в is_duplicate
        if match:
            c.execute("ROLLBACK")
            return False, match
        c.execute("INSERT INTO topics(niche,topic,fingerprint,lang,status,created,reserved_at) "
                  "VALUES(?,?,?,?,?,?,?)",
                  (niche, topic, fp_str, lang, "reserved", now, now))
        c.execute("COMMIT")
        return True, ""
    except sqlite3.OperationalError as e:        # БД занята дольше busy_timeout — не блокируем сборку
        try:
            c.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        core.log_error("reserve_topic", e, niche=niche)
        return False, ""
    finally:
        c.close()


def commit_topic(niche: str, topic: str, lang: str = "ru", dir: str = "",
                 hook: str = "", virality: int = 0) -> None:
    """Перевести зарезервированную тему в status='built' по факту успешной сборки (проставить dir/hook/virality).
    Матчим по отпечатку (устойчиво к мелким правкам темы редактором) + нише + status='reserved'."""
    if not topic:
        return
    fp_str = " ".join(sorted(_fingerprint(topic)))
    c = _conn()
    try:
        cur = c.execute(
            "UPDATE topics SET status='built', dir=?, hook=?, virality=? "
            "WHERE niche=? AND fingerprint=? AND status='reserved'",
            (dir, hook, int(virality or 0), niche, fp_str))
        if cur.rowcount == 0:           # резерва нет (старый путь/протух) — запишем как обычный built
            c.execute("INSERT INTO topics(niche,topic,fingerprint,lang,dir,hook,virality,status,created) "
                      "VALUES(?,?,?,?,?,?,?,?,?)",
                      (niche, topic, fp_str, lang, dir, hook, int(virality or 0), "built",
                       core._now().isoformat()))
        c.commit()
    except sqlite3.OperationalError as e:
        core.log_error("commit_topic", e, niche=niche)
    finally:
        c.close()


def release_topic(niche: str, topic: str) -> None:
    """Освободить тему при браке сборки: удалить запись status='reserved' (тема снова доступна)."""
    if not topic:
        return
    fp_str = " ".join(sorted(_fingerprint(topic)))
    c = _conn()
    try:
        c.execute("DELETE FROM topics WHERE niche=? AND fingerprint=? AND status='reserved'",
                  (niche, fp_str))
        c.commit()
    except sqlite3.OperationalError as e:
        core.log_error("release_topic", e, niche=niche)
    finally:
        c.close()


def recent_titles(days: int = 30, niche: str | None = None, limit: int = 60) -> list[str]:
    """Список недавних тем (для подсказки LLM «не повторяй»). Кросс-нишево по умолчанию."""
    cutoff = (core._now() - __import__("datetime").timedelta(days=days)).isoformat()
    c = _conn()
    try:
        if niche:
            rows = c.execute("SELECT topic FROM topics WHERE created>=? AND niche=? ORDER BY id DESC LIMIT ?",
                             (cutoff, niche, limit)).fetchall()
        else:
            rows = c.execute("SELECT topic FROM topics WHERE created>=? ORDER BY id DESC LIMIT ?",
                             (cutoff, limit)).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        c.close()
    return [r["topic"] for r in rows if r["topic"]]


def recent_media(niche: str, days: int = 21, limit: int = 400) -> set:
    """Ключи сток-клипов, уже использованных в нише за `days` дней (чтобы не повторять видеоряд)."""
    cutoff = (core._now() - __import__("datetime").timedelta(days=days)).isoformat()
    c = _conn()
    try:
        rows = c.execute("SELECT media_key FROM used_media WHERE niche=? AND created>=? ORDER BY id DESC LIMIT ?",
                         (niche, cutoff, limit)).fetchall()
    except sqlite3.OperationalError:
        return set()
    finally:
        c.close()
    return {r["media_key"] for r in rows if r["media_key"]}


def record_media(niche: str, keys) -> None:
    """Запомнить использованные в ролике сток-клипы (для дедупа между роликами)."""
    keys = [str(k) for k in (keys or []) if k]
    if not keys:
        return
    c = _conn()
    ts = core._now().isoformat()
    c.executemany("INSERT INTO used_media(niche, media_key, created) VALUES(?,?,?)",
                  [(niche, k, ts) for k in keys])
    c.commit()
    c.close()


def stats() -> dict:
    c = _conn()
    try:
        total = c.execute("SELECT COUNT(*) FROM topics").fetchone()[0]
        by_niche = {r["niche"]: r["n"] for r in
                    c.execute("SELECT niche, COUNT(*) n FROM topics GROUP BY niche ORDER BY n DESC")}
    except sqlite3.OperationalError:
        return {"total": 0, "by_niche": {}}
    finally:
        c.close()
    return {"total": total, "by_niche": by_niche}


if __name__ == "__main__":
    core.load_local_secrets()
    init()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "stats"
    if cmd == "stats":
        print(stats())
    elif cmd == "check" and len(sys.argv) > 2:
        dup, match = is_duplicate(" ".join(sys.argv[2:]))
        print(f"дубль: {dup}" + (f' (похоже на: «{match}»)' if dup else ""))
    elif cmd == "recent":
        for t in recent_titles(days=30):
            print(" -", t)
