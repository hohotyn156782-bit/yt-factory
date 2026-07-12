"""Оркестратор автопостинга YT Factory — YouTube-only завод (документалки + Shorts).

Выходы (output), крон разносит по неделе:
  long   — документалка 10-12 мин (ниши format=long) → YouTube
  shorts — вертикальный Short (ниши format=short) → YouTube

Публикация:
  YT_AUTO=1 (когда пройден аудит YouTube API) → прямой videos.insert + SRT + обложка.
  иначе (мост до аудита) → карточка владельцу в TG (title/description/обложка); сам файл
  остаётся в publish/, CI подхватывает его артефактом (ссылка на ран — в карточке),
  владелец выкладывает руками в Studio. ⚠️ До аудита НЕ включать YT_AUTO: видео,
  загруженные неаудированным API-проектом, навсегда блокируются в private.

Запуск: factory.py run <output> [niche].
"""
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import core  # noqa: E402
from pipeline import build as builder  # noqa: E402


def _eng_question(sc: dict) -> str:
    """1-й коммент = вовлекающий вопрос зрителю (EN) — дешёвый буст вовлечённости."""
    try:
        from pipeline import llm
        q = llm.chat("Write ONE short engaging question to viewers about this video's topic, "
                     "inviting them to answer in the comments. Question only, no quotes.",
                     f"Topic: {sc.get('topic', '')}\nHook: {sc.get('hook', '')}",
                     temp=0.8, max_tokens=80)
        return (q or "").strip().strip('"') or "What do you think? Tell us in the comments 👇"
    except Exception:  # noqa: BLE001
        return "What do you think? Tell us in the comments 👇"


def _url(info):
    return info.get("url") if isinstance(info, dict) else info


# ───────── надёжность: идемпотентность, ретраи, леджер публикаций ─────────
_STATE = core.ROOT / "state"
_POSTED = _STATE / "posted.json"
_LEDGER = _STATE / "posts.jsonl"


def _atomic_write(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    import os
    os.replace(tmp, path)


def _load_posted() -> dict:
    try:
        return json.loads(_POSTED.read_text(encoding="utf-8")) if _POSTED.exists() else {}
    except Exception as e:  # noqa: BLE001
        core.log_error("autopilot._load_posted", e)
        return {}


def already_posted(output: str, niche_id: str) -> bool:
    """Этот выход+ниша уже опубликованы СЕГОДНЯ? (анти-дубль при re-run/двойном кроне)."""
    day = core.today_str()[:10]
    return niche_id in (_load_posted().get(day, {}).get(output, []))


def _mark_posted(output: str, niche_id: str) -> None:
    day = core.today_str()[:10]
    d = _load_posted()
    d.setdefault(day, {}).setdefault(output, [])
    if niche_id not in d[day][output]:
        d[day][output].append(niche_id)
    for old in sorted(d.keys())[:-7]:      # держим только последние 7 дней
        d.pop(old, None)
    try:
        _atomic_write(_POSTED, json.dumps(d, ensure_ascii=False, indent=2))
    except Exception as e:  # noqa: BLE001
        core.log_error("autopilot._mark_posted", e)


def _entry(platform: str, account: dict, ok: bool, info) -> dict:
    """Запись леджера с ref (media_id/post_id для метрик) + secret_ref (ИМЯ env-токена, не значение)."""
    url = _url(info)
    ref = None
    if isinstance(info, dict):
        ref = info.get("id") or info.get("post_id")
    return {"platform": platform, "account": account.get("display_name") or account.get("ext_id"),
            "ok": ok, "url": url, "ref": ref,
            "secret_ref": account.get("secret_ref"), "ext_id": account.get("ext_id")}


def _ledger(output: str, niche_id: str, topic: str, entries: list) -> None:
    """Дописать УСПЕШНЫЕ публикации в state/posts.jsonl — фундамент аналитики (метрики по ref/url позже).
    entries — список dict от _entry() (или простых {'ok','url','platform','account'})."""
    day = core.today_str()
    rows = [json.dumps({"ts": day, "output": output, "niche": niche_id, "topic": topic, **e},
                       ensure_ascii=False)
            for e in entries if e.get("ok")]
    if not rows:
        return
    try:
        _STATE.mkdir(parents=True, exist_ok=True)
        with _LEDGER.open("a", encoding="utf-8") as f:
            f.write("\n".join(rows) + "\n")
    except Exception as e:  # noqa: BLE001
        core.log_error("autopilot._ledger", e)


_TRANSIENT = ("timeout", "timed out", "429", "500", "502", "503", "504",
              "temporarily", "connection", "reset", "rate limit", "try again")


def _retry_pub(fn, *args, attempts: int = 3, delays=(15, 45, 120)):
    """Публикация с ретраем ТОЛЬКО транзиентных сбоев (сеть/429/5xx). Постоянные (bad token) не ретраим."""
    import time
    ok, info = False, None
    for i in range(attempts):
        try:
            ok, info = fn(*args)
        except Exception as e:  # noqa: BLE001
            ok, info = False, str(e)[:160]
        if ok:
            return ok, info
        if not any(t in str(info).lower() for t in _TRANSIENT) or i == attempts - 1:
            return ok, info
        time.sleep(delays[min(i, len(delays) - 1)])
    return ok, info


def _qa_alert(output: str, niche_id: str, qa: dict) -> None:
    """Собранный ролик не прошёл QA → слот дня потерян. Критичный алерт владельцу (а не тихий ❌)."""
    try:
        import reporter
        why = ", ".join(qa.get("issues") or []) or "причина не указана"
        reporter.critical(f"⚠️ QA-брак · {output}/{niche_id}: {why}. Ролик собран, но не опубликован.")
    except Exception:  # noqa: BLE001
        pass


def _run_url() -> str:
    """Ссылка на текущий GitHub Actions ран (там артефакт с видео) — для карточки моста."""
    if os.environ.get("GITHUB_RUN_ID"):
        return (f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/"
                f"{os.environ.get('GITHUB_REPOSITORY', '')}/actions/runs/{os.environ['GITHUB_RUN_ID']}")
    return ""


def post_youtube(niche_id: str) -> list:
    """Собрать видео ниши (long или short) и опубликовать (YT_AUTO) / поставить в мост (TG)."""
    niche = core.get_niche(niche_id)
    fmt = niche.get("format", "short")
    attempts = int(os.environ.get("CF_MAX_ATTEMPTS") or (1 if fmt == "long" else 2))
    res = builder.build_video(niche_id, max_attempts=attempts)
    if not res or not (res.get("qa") or {}).get("ok", True):
        _qa_alert(fmt, niche_id, (res or {}).get("qa") or {})
        return [(f"{fmt}/{niche_id}", False, "QA не пройден")]
    meta = res["meta"]
    sc = res.get("script", {})
    is_short = fmt == "short"

    if os.environ.get("YT_AUTO", "0").strip() == "1":
        from adapters import youtube as yt
        ok, info = _retry_pub(yt.publish, meta["video"], meta, None, is_short)
        if ok and not is_short and meta.get("srt") and isinstance(info, dict):
            try:
                yt.upload_caption(info.get("id", ""), meta["srt"])
            except Exception as e:  # noqa: BLE001
                core.log_error("autopilot.caption", e, niche=niche_id)
        _ledger(fmt, niche_id, meta.get("topic", ""),
                [{"platform": "youtube", "account": "main", "ok": ok, "url": _url(info),
                  "ref": info.get("id") if isinstance(info, dict) else None}])
        return [(f"youtube/{niche_id}", ok, _url(info))]

    # мост до аудита: карточка владельцу в TG; файл — в publish/ → артефакт CI (ссылка в карточке)
    from adapters import tg_queue
    title = meta.get("title") or (meta.get("captions", {}).get("youtube", {}) or {}).get("title") \
        or meta.get("topic", "")
    if is_short:
        tags = " ".join("#" + t.lstrip("#") for t in meta.get("hashtags", [])[:12])
        description = (meta.get("captions", {}).get("youtube", {}) or {}).get("description", "")
    else:
        tags = ", ".join(meta.get("tags", []))
        description = meta.get("description", "")
    note = (f"\n\n📦 Файл видео: артефакт рана CI → {_run_url()}" if _run_url() else "")
    if meta.get("ai_disclosure"):
        note += "\n⚠️ AI-disclosure: при загрузке поставь галку Altered content = YES"
    if (meta.get("qa") or {}).get("forced"):
        issues = "; ".join(str(x) for x in ((meta.get("qa") or {}).get("issues") or []))[:300]
        note += f"\n🔴 QA-форс: визуальные артефакты не исправлены за все попытки — глянь видео перед выкладкой. {issues}"
    extras = {"thumb": meta.get("thumbnail"), "description": (description + note).strip()}
    ok, info = tg_queue.send_item("youtube", meta["video"], title, tags, _eng_question(sc),
                                  channel=niche.get("title", ""), niche=niche_id, extras=extras)
    _ledger(fmt, niche_id, meta.get("topic", ""),
            [{"platform": "youtube", "account": "tg-bridge", "ok": ok, "url": None, "queued": True}])
    return [(f"youtube/{niche_id}", ok, info)]


# ───────── жёсткий стоп по времени на нишу + честная ротация ниш ─────────
_CURSOR = _STATE / "niche_cursor.json"


class _NicheTimeout(BaseException):
    """Ниша превысила жёсткий лимит времени. Наследуем BaseException (а не Exception),
    чтобы прервать нишу СКВОЗЬ внутренние `except Exception` сборки/публикации, которые иначе
    проглотили бы обычное исключение и продолжили пересборку."""


def _raise_niche_timeout(signum, frame):    # обработчик SIGALRM
    raise _NicheTimeout()


def _load_cursor() -> dict:
    try:
        return json.loads(_CURSOR.read_text(encoding="utf-8")) if _CURSOR.exists() else {}
    except Exception as e:  # noqa: BLE001
        core.log_error("autopilot._load_cursor", e)
        return {}


def _save_cursor(output: str, idx: int) -> None:
    d = _load_cursor()
    d[output] = int(idx)
    try:
        _atomic_write(_CURSOR, json.dumps(d, ensure_ascii=False, indent=2))
    except Exception as e:  # noqa: BLE001
        core.log_error("autopilot._save_cursor", e)


def _rotate(items: list, start: int) -> list:
    """Список, повёрнутый так, чтобы он начинался с индекса start (round-robin по запускам)."""
    if not items:
        return items
    s = start % len(items)
    return items[s:] + items[:s]


def run(output: str, niche: str | None = None) -> list:
    if output not in ("long", "shorts"):
        raise SystemExit(f"неизвестный output: {output} (long|shorts)")
    fn = post_youtube
    want_fmt = "long" if output == "long" else "short"
    single = niche is not None
    niches = [niche] if single else [n["id"] for n in core.load_niches(only_enabled=True)
                                     if n.get("format", "short") == want_fmt]
    # Честная ротация: раньше ниши шли фиксированным порядком, и хвост списка вечно отсекался
    # бюджетом → одни и те же ниши (soviet_things/psy_stories) НИКОГДА не публиковались.
    # Стартуем с той ниши, на которой прошлый запуск оборвался по бюджету — round-robin по
    # запускам покрывает все ниши. Только для полного прогона (точечный --niche не двигает курсор).
    start = _load_cursor().get(output, 0) if not single else 0
    niches = _rotate(niches, start)

    # Бюджет по времени: не начинать новую нишу под конец лимита GitHub (timeout-minutes).
    # Иначе джоб убивают ПОСРЕДИ сборки → шаг commit-back не успевает сохранить состояние
    # (сериалы/история тем/posted.json/курсор) и назавтра дубли. Дефолт 80 мин при лимите
    # джоба 120: t0 стартует ПОСЛЕ setup (~3 мин), а после цикла ещё commit-back (~2 мин).
    import os
    import signal
    import time
    try:
        budget_s = float(os.environ.get("CF_RUN_BUDGET_S") or 4800)
    except ValueError:                    # мусор в env не должен ронять весь автопилот
        budget_s = 4800.0
    # Жёсткий лимит на ОДНУ нишу: QA-трэшинг (пересборка до 4× при браке AI-картинок) или
    # зависшая сеть не должны съедать весь джоб и ронять его в timeout-minutes, голодя остальные
    # ниши. 25 мин при бюджете 80 → худший старт 80-й мин + 25 = 105 + setup(~3) + commit-back(~2)
    # ≈ 110 < 120, есть запас. Работает только в главном потоке на Unix (CI = ubuntu).
    # long-сборка (сценарий+озвучка+рендер 10-12 мин) занимает до ~90 мин на CI —
    # шортс-кап 25 мин её гарантированно убьёт, поэтому дефолт капа зависит от выхода.
    default_cap = 5400 if output == "long" else 1500
    try:
        niche_cap_s = max(0, int(float(os.environ.get("CF_NICHE_CAP_S") or default_cap)))
    except ValueError:
        niche_cap_s = default_cap
    use_alarm = niche_cap_s > 0 and hasattr(signal, "SIGALRM")

    t0 = time.time()
    allr, skipped, processed = [], [], 0
    for n in niches:
        if time.time() - t0 > budget_s:
            skipped.append(n)
            continue
        processed += 1                    # ниша получила свой слот (пост/анти-дубль/ошибка) — курсор её минует
        print(f"— {output} · {n} —")
        if already_posted(output, n):
            print(f"  ⏭ уже опубликовано сегодня ({output}/{n}) — пропуск (анти-дубль)")
            continue
        prev_handler, armed = None, False
        if use_alarm:
            try:
                prev_handler = signal.signal(signal.SIGALRM, _raise_niche_timeout)
                signal.alarm(niche_cap_s)
                armed = True
            except (ValueError, OSError):     # не главный поток → без жёсткого таймаута
                armed = False
        try:
            rr = fn(n)
            if armed:
                signal.alarm(0)               # ниша собрана/опубликована — снимаем таймер до пост-обработки
            for lbl, ok, url in rr:
                print(f"  {'✅' if ok else '❌'} {lbl}: {url}")
            allr += rr
            if any(ok for _, ok, _ in rr):     # хоть одна площадка успешна → метим день
                _mark_posted(output, n)
        except _NicheTimeout:
            print(f"  ⏱ ниша {n} превысила лимит {niche_cap_s // 60} мин — прервана, идём дальше")
            core.log_error(f"autopilot.{output}.niche_timeout",
                           RuntimeError(f"{n} > {niche_cap_s}s (QA-трэшинг/зависание)"), niche=n)
        except Exception as e:  # noqa: BLE001
            print(f"  ❌ {n}: {str(e)[:160]}")
            core.log_error(f"autopilot.{output}", e, niche=n)
        finally:
            if armed:
                signal.alarm(0)
                try:
                    signal.signal(signal.SIGALRM, prev_handler if prev_handler is not None else signal.SIG_DFL)
                except (ValueError, OSError, TypeError):
                    pass
    # Курсор: следующий запуск начнёт с первой НЕобработанной (отсечённой бюджетом) ниши.
    if not single and niches:
        _save_cursor(output, (start + processed) % len(niches))
    if skipped:
        # НЕ молча: явно сообщаем, до каких ниш не дошли в бюджете времени (получат слот в след. запуске).
        print(f"⏳ бюджет времени исчерпан — пропущено ниш: {len(skipped)}: {', '.join(skipped)}")
        try:
            import reporter
            reporter.critical(f"⏳ Автопилот {output}: не хватило времени на {len(skipped)} ниш "
                              f"({', '.join(skipped)}). Пойдут в следующий запуск.")
        except Exception:  # noqa: BLE001
            pass
    okn = sum(1 for _, ok, _ in allr if ok)
    try:
        import reporter
        reporter.send(f"🤖 <b>Автопилот v2: {output}</b> — ок {okn}/{len(allr)}")
    except Exception:  # noqa: BLE001
        pass
    return allr
