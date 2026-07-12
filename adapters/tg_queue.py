"""TG-очередь ручной выкладки YouTube/TikTok.

Бот (TG_QUEUE_BOT_TOKEN) шлёт админу: видеофайл + сообщение с готовой копией (заголовок/теги/
1-й коммент) и inline-кнопкой «✅ Опубликовано». YouTube → Паше, TikTok → Даше.
Кнопку обрабатывает Vercel-вебхук (см. webhook/api/done.js). Сам отправитель работает из CI без вебхука.
"""
import os
import json
import time
import pathlib
import sys

import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import core  # noqa: E402

_API = "https://api.telegram.org/bot{token}/{method}"
# дефолтные чаты (можно переопределить env TG_QUEUE_CHAT_YOUTUBE / _TIKTOK)
_ADMINS = {"youtube": "964216249", "tiktok": "1692866818"}
_TG_FILE_LIMIT = 49 * 1024 * 1024   # Bot API sendVideo ~50МБ; берём с запасом


def _token() -> str:
    return os.environ.get("TG_QUEUE_BOT_TOKEN", "").strip()


def _chat(target: str) -> str:
    return (os.environ.get(f"TG_QUEUE_CHAT_{target.upper()}", "").strip()
            or _ADMINS.get(target, "")).strip()


def admin_chats() -> list[str]:
    """Оба админа получают ОБА выхода (и YouTube, и TikTok): Паша + Даша. Дедуп, без пустых.
    env-переопределение: TG_QUEUE_CHAT_YOUTUBE / _TIKTOK, плюс TG_QUEUE_CHAT_ALL (через запятую)."""
    extra = [c.strip() for c in os.environ.get("TG_QUEUE_CHAT_ALL", "").split(",") if c.strip()]
    out: list[str] = []
    for c in [_chat("youtube"), _chat("tiktok"), *extra]:
        c = (c or "").strip()
        if c and c not in out:
            out.append(c)
    return out


def _req(tok: str, method: str, *, data=None, files=None, attempts: int = 3):
    """POST в Bot API с ретраями на 429 (respect retry_after) и 5xx/сеть. Возвращает dict-ответ Telegram."""
    last = {"ok": False, "description": "нет попыток"}
    for i in range(attempts):
        try:
            r = requests.post(_API.format(token=tok, method=method),
                              data=data, files=files, timeout=240)
            try:
                j = r.json()
            except ValueError:  # не-JSON (413/HTML) — не ретраим тело, вернём статус
                return {"ok": False, "description": f"HTTP {r.status_code} (не-JSON ответ)"}
            if j.get("ok"):
                return j
            # 429 — ждём retry_after; 5xx — бэкофф; прочее (400/403) — не ретраить
            code = j.get("error_code")
            if code == 429:
                wait = int((j.get("parameters") or {}).get("retry_after", 3))
                time.sleep(min(wait, 30))
            elif code and 500 <= code < 600:
                time.sleep(2 * (i + 1))
            else:
                return j
            last = j
        except requests.RequestException as e:
            last = {"ok": False, "description": str(e)[:160]}
            time.sleep(2 * (i + 1))
    return last


def send_item(target: str, video_path: str, title: str, tags: str,
              first_comment: str, channel: str = "", niche: str = "",
              extras: dict | None = None, chat_override: str = "") -> tuple[bool, str]:
    """target: 'youtube' (→Паше) | 'tiktok' (→Даше). Шлёт видео + копию + кнопку. Возвращает (ok, info).
    Успех = доставлена КОПИЯ С КНОПКОЙ (без неё трекинг готовности невозможен). Видео — best-effort:
    при >49МБ файл в TG не влезает → заливаем на media_host и даём ссылку в копии.
    extras (опц.): {'thumb': путь_к_обложке, 'title_variants': [...], 'description': '...'} —
    обогащают ручную выкладку (A/B-заголовки, готовое описание, обложку отдельным фото)."""
    extras = extras or {}
    tok = _token()
    if not tok:
        return False, "нет TG_QUEUE_BOT_TOKEN"
    chat = (chat_override or _chat(target)).strip()
    if not chat:
        return False, f"нет chat_id для {target}"
    head = {"youtube": "▶️ YouTube Shorts", "tiktok": "🎵 TikTok"}.get(target, target.upper())

    # 1) видеофайл — только если влезает; иначе фолбэк на ссылку media_host
    video_note = ""
    try:
        size = os.path.getsize(video_path)
    except OSError as e:
        return False, f"нет видеофайла: {str(e)[:120]}"
    if size <= _TG_FILE_LIMIT:
        with open(video_path, "rb") as f:
            rv = _req(tok, "sendVideo",
                      data={"chat_id": chat, "caption": f"{head} · {channel or niche}",
                            "supports_streaming": "true"},
                      files={"video": f})
        if not rv.get("ok"):
            video_note = "⚠️ видео не отправилось (" + str(rv.get("description"))[:80] + ") — см. ссылку ниже\n"
    else:
        video_note = f"⚠️ видео {size/1e6:.0f}МБ — не влезло в TG\n"
    # фолбэк-ссылка на скачивание, если файл не ушёл в TG
    if video_note:
        try:
            from adapters import media_host
            url = media_host.public_url(video_path)
            video_note += f"⬇️ Скачать: {url}\n"
        except Exception as e:  # noqa: BLE001
            video_note += f"(ссылку сделать не удалось: {str(e)[:80]}; путь: {video_path})\n"

    # 1b) обложка отдельным фото (для YouTube — можно поставить вручную)
    thumb = extras.get("thumb")
    if thumb:
        try:
            if os.path.getsize(thumb) <= _TG_FILE_LIMIT:
                with open(thumb, "rb") as f:
                    _req(tok, "sendPhoto", data={"chat_id": chat, "caption": "🖼 обложка (A/B-победитель)"},
                         files={"photo": f})
        except OSError:
            pass

    # 2) готовая копия + кнопка «опубликовано» — критична, ретраим внутри _req
    variants = extras.get("title_variants") or []
    vblock = ""
    if variants:
        vlines = "\n".join(f"• <code>{_esc(v)}</code>" for v in variants[:5] if v)
        if vlines:
            vblock = f"\n\n🅰️🅱️ <b>Ещё заголовки (A/B):</b>\n{vlines}"
    desc = (extras.get("description") or "").strip()
    dblock = f"\n\n📝 <b>Описание:</b>\n<code>{_esc(desc[:600])}</code>" if desc else ""
    body = ((video_note + "\n") if video_note else "") + (
        f"{head} · <b>{_esc(channel or niche)}</b>\n\n"
        f"📌 <b>Заголовок:</b>\n<code>{_esc(title)}</code>{vblock}\n\n"
        f"🏷 <b>Теги:</b>\n<code>{_esc(tags)}</code>{dblock}\n\n"
        f"💬 <b>1-й коммент (закрепить):</b>\n<code>{_esc(first_comment)}</code>")
    body = body[:4096]
    kb = {"inline_keyboard": [[{"text": "✅ Опубликовано",
                                "callback_data": f"done:{target}:{niche}"[:60]}]]}
    rm = _req(tok, "sendMessage",
              data={"chat_id": chat, "text": body, "parse_mode": "HTML",
                    "reply_markup": json.dumps(kb)})
    if not rm.get("ok"):
        core.log_error("tg_queue.copy", RuntimeError(str(rm.get("description"))[:160]), niche=niche)
        return False, "копия с кнопкой не доставлена: " + str(rm.get("description"))[:120]
    return True, f"в TG ({target})" + (" [видео ссылкой]" if video_note else "")


def _esc(s: str) -> str:
    return str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
