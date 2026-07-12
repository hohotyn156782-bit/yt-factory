"""QA-тестер ролика ПОСЛЕ рендера и ДО публикации.

Две группы проверок:
  • Технические (ffprobe/ffmpeg): рассинхрон аудио↔видео, фриз кадра (freezedetect),
    разрешение/ориентация, наличие и длина аудио, битый файл.
  • Визуальные (AI-зрение Gemini, мульти-ключ): кадры на аномалии — деформированные
    лица/руки/лишние пальцы, искажённые тела, нечитаемый «AI»-текст, артефакты.

check(video) → {"ok": bool, "issues": [...], "technical": {...}, "visual": {...}}.
Если Gemini недоступен (429/нет ключей) — визуальная часть мягко пропускается (ok по технике).
"""
import os
import json
import base64
import subprocess
import pathlib

import sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import core  # noqa: E402

VISION_MODEL = "gemini-2.5-flash"


def _probe(video: str) -> dict:
    def q(*a):
        try:
            return subprocess.run(["ffprobe", "-v", "error", *a, video],
                                  capture_output=True, text=True, timeout=60).stdout.strip()
        except Exception:  # noqa: BLE001
            return ""
    vdur = q("-select_streams", "v:0", "-show_entries", "stream=duration,width,height,r_frame_rate",
             "-of", "json")
    adur = q("-select_streams", "a:0", "-show_entries", "stream=duration", "-of", "json")
    try:
        v = json.loads(vdur).get("streams", [{}])[0]
    except Exception:  # noqa: BLE001
        v = {}
    try:
        a = json.loads(adur).get("streams", [{}])
        a = a[0] if a else {}
    except Exception:  # noqa: BLE001
        a = {}
    return {"v": v, "a": a}


def _scan_timeout(vdur: float) -> int:
    """Таймаут одного ffmpeg-прохода по видео: ~2x реального времени + запас.
    Донорские фиксированные 120-180с рассчитаны на shorts ≤60с — 10-минутную
    документалку такой прогон не успевает декодировать."""
    return int(vdur * 2 + 120)


def _freeze_segments(video: str, timeout: int = 180) -> tuple[list[float], bool]:
    """Длительности замёрзших участков (через freezedetect).
    Возвращает (durs, ok): ok=False при сбое/таймауте детекции (результат недостоверен,
    пустой список НЕ означает «фризов нет»). Пустой при ok=True = фризов нет."""
    try:
        r = subprocess.run(
            ["ffmpeg", "-i", video, "-vf", "freezedetect=n=-60dB:d=1.2", "-map", "0:v",
             "-f", "null", "-"], capture_output=True, text=True, timeout=timeout)
    except Exception as e:  # noqa: BLE001 — таймаут/исключение: детекция не завершилась
        core.log("QA: детекция фриза не завершилась — пропущена",
                 level="warn", video=pathlib.Path(video).name, error=str(e))
        return [], False
    if r.returncode != 0:   # ненулевой код = ffmpeg оборвался, результат недостоверен
        core.log("QA: детекция фриза не завершилась — пропущена",
                 level="warn", video=pathlib.Path(video).name, returncode=r.returncode)
        return [], False
    durs = []
    for line in r.stderr.splitlines():
        if "freeze_duration" in line:
            try:
                durs.append(float(line.split("freeze_duration:")[1].split()[0]))
            except Exception:  # noqa: BLE001
                pass
    return durs, True


def _scene_cuts(video: str, timeout: int = 180) -> tuple[list[float], bool]:
    """Таймкоды смен кадра (scene-detection). Для контроля плотности нарезки/интро (удержание).
    Возвращает (times, ok): ok=False при сбое/таймауте детекции (результат недостоверен,
    пустой список НЕ означает «нарезки нет»)."""
    try:
        r = subprocess.run(
            ["ffmpeg", "-i", video, "-vf", "select='gt(scene,0.3)',showinfo", "-an", "-f", "null", "-"],
            capture_output=True, text=True, timeout=timeout)
    except Exception as e:  # noqa: BLE001 — таймаут/исключение: детекция не завершилась
        core.log("QA: детекция нарезки не завершилась — пропущена",
                 level="warn", video=pathlib.Path(video).name, error=str(e))
        return [], False
    if r.returncode != 0:   # ненулевой код = ffmpeg оборвался, результат недостоверен
        core.log("QA: детекция нарезки не завершилась — пропущена",
                 level="warn", video=pathlib.Path(video).name, returncode=r.returncode)
        return [], False
    times = []
    for line in r.stderr.splitlines():
        if "pts_time:" in line and "showinfo" in line:
            try:
                times.append(float(line.split("pts_time:")[1].split()[0]))
            except Exception:  # noqa: BLE001
                pass
    return times, True


def _mean_volume(video: str, timeout: int = 120) -> float:
    """Средняя громкость (dB) через volumedetect. Очень низкая (~-90) = тишина/битый звук."""
    try:
        r = subprocess.run(["ffmpeg", "-i", video, "-af", "volumedetect", "-f", "null", "-"],
                           capture_output=True, text=True, timeout=timeout)
        for line in r.stderr.splitlines():
            if "mean_volume:" in line:
                return float(line.split("mean_volume:")[1].split("dB")[0].strip())
        core.log("QA: volumedetect не вернул mean_volume — замер громкости провалился (fail-closed)",
                 level="warn", video=pathlib.Path(video).name)
    except Exception as e:  # noqa: BLE001
        core.log("QA: сбой замера громкости (volumedetect) — fail-closed",
                 level="warn", video=pathlib.Path(video).name, error=str(e))
    return -999.0  # fail-CLOSED: сбой/таймаут замера трактуется как тишина → гейт `< -70` сработает


def _duration_window(niche: dict | None) -> tuple[float, float]:
    """Допустимое окно длительности по нише. long: дефолт 480-900с, target_minutes [lo,hi]
    сужает окно (lo*60-120 … hi*60+180). short: донорское 8с … max_seconds (или 70)."""
    fmt = (niche or {}).get("format") or core.ACTIVE_FORMAT
    if fmt == "long":
        tm = (niche or {}).get("target_minutes")
        if isinstance(tm, (list, tuple)) and len(tm) == 2:
            return max(60.0, tm[0] * 60 - 120), tm[1] * 60 + 180
        return 480.0, 900.0
    return 8.0, float((niche or {}).get("max_seconds") or 70)


def check_technical(video: str, niche: dict | None = None) -> dict:
    issues = []
    p = _probe(video)
    v, a = p["v"], p["a"]
    vdur = float(v.get("duration") or 0)
    adur = float(a.get("duration") or 0)
    w, h = int(v.get("width") or 0), int(v.get("height") or 0)
    scan_to = _scan_timeout(vdur)   # таймаут полного декод-прохода масштабируется от длительности

    if not a:
        issues.append("нет аудио-дорожки")
    else:
        if _mean_volume(video, timeout=scan_to) < -70:    # практически тишина → сбой озвучки/микса
            issues.append("аудио почти тишина (mean_volume < -70dB) — сбой озвучки")
        # асимметрично: видео ШТАТНО длиннее аудио на ~0.6с (хвост последнего клипа, assemble.py) —
        # это норма. ОПАСНО только когда АУДИО длиннее видео (обрыв звука/фриз в конце — тот самый
        # десинк-баг) → ловим жёстко (>0.25с). Видео длиннее аудио флагуем лишь при явном переборе.
        if adur and vdur:
            if adur - vdur > 0.25:
                issues.append(f"аудио длиннее видео на {adur - vdur:.2f}с — обрыв звука/фриз в конце "
                              f"(видео {vdur:.1f}с vs аудио {adur:.1f}с)")
            elif vdur - adur > 1.2:
                issues.append(f"видео длиннее аудио на {vdur - adur:.2f}с — лишний хвост/подвисание "
                              f"(видео {vdur:.1f}с vs аудио {adur:.1f}с)")
    if (w, h) != (core.W, core.H):
        issues.append(f"разрешение {w}x{h}, нужно {core.W}x{core.H}")
    # окно длительности по формату ниши: short 8с…max_seconds, long 480-900с (или target_minutes)
    min_s, max_s = _duration_window(niche)
    if vdur < min_s:
        issues.append(f"подозрительно короткий ролик {vdur:.1f}с (минимум {min_s:.0f}с)")
    # верхняя граница: у short ловит баг рендера (зацикленное аудио/дубль → 70-120с),
    # у long — раздутый сценарий за пределами таргета
    if vdur > max_s:
        issues.append(f"слишком длинный ролик {vdur:.1f}с (максимум {max_s:.0f}с)")
    frz, freeze_ok = _freeze_segments(video, timeout=scan_to)
    # порог «настоящего фриза» зависит от формата: в документалке 16:9 спокойные планы
    # 6-14с и медленный Ken Burns — норма жанра; блокируем только реально мёртвые куски
    # (длиннее максимального слота) или когда статики суммарно слишком много.
    if (niche or {}).get("format") == "long":
        big = [d for d in frz if d >= 16.0]
        if big:
            issues.append(f"замирание кадра: {', '.join(f'{d:.1f}с' for d in big)}")
        elif vdur and sum(frz) > 0.45 * vdur:
            issues.append(f"статика {sum(frz):.0f}с из {vdur:.0f}с (>45%) — видео почти не движется")
    else:
        big = [d for d in frz if d >= 1.5]   # настоящий фриз; <1.5с бывает у плавного параллакса
        if big:
            issues.append(f"замирание кадра: {', '.join(f'{d:.1f}с' for d in big)}")
    # cut-rate: плотность нарезки + плотность интро (0-3с) — НЕ блокирует, только сигнал удержания
    cuts, cuts_ok = _scene_cuts(video, timeout=scan_to)
    intro_cuts = sum(1 for t in cuts if t <= 3.0)
    cut_rate = round(len(cuts) / vdur, 2) if vdur else 0.0
    if cuts_ok and vdur > 10 and intro_cuts < 2:
        core.log("QA: разреженное интро (<2 смен кадра в первые 3с) — риск дроп-оффа",
                 level="warn", video=pathlib.Path(video).name, intro_cuts=intro_cuts)
    # #19 fail-OPEN прозрачность: сбой детекции (таймаут/код≠0) не блокирует, но помечается unverified
    freeze_unverified = not freeze_ok
    cuts_unverified = not cuts_ok
    return {"ok": not issues, "issues": issues,
            "video_dur": round(vdur, 2), "audio_dur": round(adur, 2), "res": f"{w}x{h}",
            "cuts": len(cuts), "cut_rate": cut_rate, "intro_cuts": intro_cuts,
            "freeze_unverified": freeze_unverified, "cuts_unverified": cuts_unverified}


def _frames(video: str, vdur: float, workdir: pathlib.Path, n: int = 4) -> list[pathlib.Path]:
    workdir.mkdir(parents=True, exist_ok=True)
    out = []
    for i in range(n):
        t = vdur * (i + 0.5) / n
        fp = workdir / f"qa_{i}.jpg"
        try:
            subprocess.run(["ffmpeg", "-y", "-ss", f"{t:.2f}", "-i", video, "-frames:v", "1",
                            "-vf", "scale=540:-1", str(fp)], capture_output=True, timeout=40)
            if fp.exists():
                out.append(fp)
        except Exception:  # noqa: BLE001
            pass
    return out


def check_visual(video: str, vdur: float, workdir: pathlib.Path, niche: dict | None = None) -> dict:
    """AI-зрение по кадрам. Возвращает {ok, issues, checked}. Мягко пропускает при отсутствии Gemini.
    Число кадров зависит от формата: short — 4, long — 8 (равномерно по всему хронометражу)."""
    keys = [k.strip() for k in os.environ.get("GEMINI_API_KEY", "").split(",") if k.strip()]
    if not keys:
        return {"ok": True, "issues": [], "checked": False, "note": "нет Gemini-ключей"}
    fmt = (niche or {}).get("format") or core.ACTIVE_FORMAT
    frames = _frames(video, vdur, workdir, n=(8 if fmt == "long" else 4))
    if not frames:
        return {"ok": True, "issues": [], "checked": False, "note": "не извлёк кадры"}
    parts = [{"text": (
        "These are frames from a video. Find VISUAL DEFECTS that would make the video unpublishable: "
        "deformed/distorted faces, malformed hands, extra or fused fingers/limbs, "
        "unnatural bodies, unreadable or garbled text/mangled letters in the frame, gross AI artifacts, "
        "completely blank/black frames. Do NOT count styling or cosmetics as a defect. "
        'Return STRICTLY JSON: {"ok": true|false, "issues": ["short defect description", ...]}. '
        "ok=false only for obvious gross defects.")}]
    for fp in frames:
        parts.append({"inline_data": {"mime_type": "image/jpeg",
                                      "data": base64.b64encode(fp.read_bytes()).decode()}})
    body = json.dumps({"contents": [{"parts": parts}],
                       "generationConfig": {"responseMimeType": "application/json"}}).encode()
    # #7: строгий промпт + БЕЗ responseMimeType — для retry того же ключа, когда HTTP-ответ
    # пришёл, но текст не распарсился в JSON (модель «заболтала» ответ, обёрнутый в ```json и т.п.).
    strict_parts = list(parts)
    strict_parts[0] = {"text": parts[0]["text"] +
                       " IMPORTANT: return ONLY valid JSON, no markdown, no explanations, no ```."}
    strict_body = json.dumps({"contents": [{"parts": strict_parts}]}).encode()
    import urllib.request

    def _ask(key: str, payload: bytes) -> str:
        """Один запрос к ключу. Возвращает извлечённый text (может быть пустым).
        Сетевые/429/исключения запроса пробрасываются наружу (трактуются как промах ключа)."""
        # ВАЖНО: новые ключи Google формата `AQ.…` работают ТОЛЬКО через заголовок x-goog-api-key.
        # Старый `?key=` даёт им 401 ACCESS_TOKEN_TYPE_UNSUPPORTED → визуальный QA молча отваливался.
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{VISION_MODEL}:generateContent"
        req = urllib.request.Request(url, data=payload,
                                     headers={"Content-Type": "application/json", "x-goog-api-key": key})
        r = json.loads(urllib.request.urlopen(req, timeout=90).read())
        cand = (r.get("candidates") or [{}])[0]
        parts = (cand.get("content") or {}).get("parts") or [{}]
        return parts[0].get("text", "") or ""

    for key in keys:
        # 1) Сетевой слой: пустой ответ / исключение запроса / 429 → fail-open, continue к след. ключу.
        try:
            txt = _ask(key, body)
        except Exception:  # noqa: BLE001 — настоящий сетевой промах ключа: норм fail-open
            continue
        if not txt or not txt.strip():
            continue  # пустой ответ при валидном HTTP — трактуем как промах ключа, след. ключ
        # 2) Ответ НЕПУСТОЙ. Парсинг JSON — отдельная ветка (НЕ сваливаем в сетевую кучу).
        try:
            d = json.loads(txt)
            return {"ok": bool(d.get("ok", True)), "issues": d.get("issues", []),
                    "checked": True, "frames": len(frames)}
        except (json.JSONDecodeError, TypeError):
            pass  # ключ РАБОТАЕТ (квота потрачена), но JSON битый → ОДИН retry тем же ключом
        try:
            txt2 = _ask(key, strict_body)
            d = json.loads(txt2)
            return {"ok": bool(d.get("ok", True)), "issues": d.get("issues", []),
                    "checked": True, "frames": len(frames)}
        except (json.JSONDecodeError, TypeError):
            # Дважды непарсибельный JSON при рабочем ключе — это РЕАЛЬНЫЙ провал проверки,
            # а не сетевой промах. Логируем на error (не молчим) и помечаем visual_unverified.
            core.log("QA: визуальная проверка — ответ Gemini не парсится в JSON (ключ рабочий, квота потрачена)",
                     level="error", video=pathlib.Path(video).name, txt=str(txt2)[:200] or str(txt)[:200])
            return {"ok": True, "issues": [], "checked": False, "visual_unverified": True,
                    "note": "Gemini вернул невалидный JSON (проверка не пройдена)"}
        except Exception as e:  # noqa: BLE001 — на retry упала сеть/429: вернуть в fail-open поток
            core.log_error("QA.check_visual retry", e, video=pathlib.Path(video).name)
            continue
    return {"ok": True, "issues": [], "checked": False, "note": "Gemini-зрение недоступно (429/ошибка)"}


def check(video: str, workdir: pathlib.Path | None = None, niche: dict | None = None) -> dict:
    workdir = workdir or pathlib.Path(video).parent / "_qa"
    tech = check_technical(video, niche=niche)
    vis = check_visual(video, tech["video_dur"], workdir, niche=niche)
    issues = ([f"[тех] {i}" for i in tech["issues"]] + [f"[вид] {i}" for i in vis.get("issues", [])])
    ok = tech["ok"] and vis.get("ok", True)
    visual_unverified = not vis.get("checked", False)   # #5: Gemini не проверил картинку (нет ключа/429)
    if visual_unverified and ok:
        core.log("QA: визуальная проверка НЕ выполнена (Gemini недоступен) — публикуется без AI-ревью кадров",
                 level="warn", video=pathlib.Path(video).name)
    # #19: проброс флагов недостоверной тех-детекции в meta (fail-OPEN, не блокирует)
    freeze_unverified = tech.get("freeze_unverified", False)
    cuts_unverified = tech.get("cuts_unverified", False)
    return {"ok": ok, "issues": issues, "visual_unverified": visual_unverified,
            "freeze_unverified": freeze_unverified, "cuts_unverified": cuts_unverified,
            "technical": tech, "visual": vis}


if __name__ == "__main__":
    core.load_local_secrets()
    vid = sys.argv[1]
    print(json.dumps(check(vid), ensure_ascii=False, indent=2))
