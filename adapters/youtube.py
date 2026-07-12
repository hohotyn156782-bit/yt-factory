"""Загрузка видео (long-form 16:9 и Shorts) на YouTube через Data API v3 (СЕТЬ каналов).

Контракт short/long: publish(..., is_short=) — явный параметр приоритетнее всего;
is_short=None → meta["format"] → format ниши из niches.json → core.ACTIVE_FORMAT.
"#Shorts" дописывается в описание ТОЛЬКО для shorts. publish_at (datetime|ISO-строка) →
privacyStatus=private + status.publishAt (RFC3339 UTC; наивный datetime трактуем как core.TZ).
Субтитры: upload_caption() — captions().insert, не-фатально (нужен scope youtube.force-ssl).

Мульти-канальность (как vk_video._account_target): у каждого аккаунта панели токен
живёт в отдельном файле core.DATA_ROOT/"yt_tokens"/<name>.json, где
name = account['secret_ref'] (или account['name'], если secret_ref пуст).
Если per-channel файла нет — ФОЛБЭК на одноканальный yt_token.json (обратная совместимость).

Требует OAuth Installed-App токен (см. youtube_auth.py — запустить один раз локально
на КАЖДЫЙ канал сети: python3 adapters/youtube_auth.py <name>).
Env:
  YT_CLIENT_SECRET_FILE — путь к client_secret.json (GCP OAuth Desktop client)
  YT_TOKEN_FILE         — путь к одноканальному token.json (дефолт/фолбэк)

КРИТИЧНО (из ресёрча):
  • Пока проект не прошёл YouTube API Compliance Audit — ВСЕ загрузки молча приватные
    (insert вернёт 201, но privacyStatus=private). Подать форму аудита сразу, ждать 2-4 недели.
  • Скрытый лимит ~7 загрузок/день/канал (с мая 2026). Фабрика держит кап на стороне оркестратора.
  • Shorts классифицируется автоматически по 9:16 + ≤180с + #Shorts в описании. Спец-эндпоинта нет.
"""
import datetime as dt
import os
import pathlib

import sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import core  # noqa: E402

# upload — видео/обложки; force-ssl — captions().insert (субтитры). Токены надо минтить
# СРАЗУ с обоими scope (youtube_auth.py), иначе upload_caption будет 403 (не-фатально).
SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.force-ssl"]
CHUNK = 262144 * 4  # 1 MiB, кратно 256 KiB (требование resumable upload)


def _imports():
    try:
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        return build, MediaFileUpload, Credentials, Request
    except ImportError:
        raise RuntimeError("Нет google-клиента. Установи: pip install --break-system-packages "
                           "google-api-python-client google-auth google-auth-oauthlib google-auth-httplib2")


def _account_name(account: dict | None) -> str:
    """Имя per-channel токена из аккаунта панели: secret_ref (приоритет) или name. Пусто — нет имени."""
    if not account:
        return ""
    return str(account.get("secret_ref") or account.get("name") or "").strip()


def _token_path(account: dict | None) -> pathlib.Path:
    """Путь к токену канала. Если у аккаунта есть имя и файл core.DATA_ROOT/yt_tokens/<name>.json
    существует — берём его (сеть каналов). Иначе ФОЛБЭК на одноканальный YT_TOKEN_FILE."""
    name = _account_name(account)
    if name:
        per = core.DATA_ROOT / "yt_tokens" / f"{name}.json"
        if per.exists():
            return per
    return pathlib.Path(os.environ.get(
        "YT_TOKEN_FILE", str(pathlib.Path("~/.config/content-factory/yt_token.json").expanduser())))


def _service(account: dict | None = None):
    build, _, Credentials, Request = _imports()
    token_file = _token_path(account)
    if not token_file.exists():
        name = _account_name(account)
        hint = f" python3 adapters/youtube_auth.py {name}" if name else " python3 -m adapters.youtube_auth"
        raise RuntimeError(f"Нет {token_file}. Запусти один раз локально:{hint}")
    # scopes НЕ форсируем: берём выданные токену (в json от creds.to_json()); старый токен
    # только с youtube.upload продолжит грузить видео, а captions отвалятся не-фатально
    creds = Credentials.from_authorized_user_file(str(token_file))
    if not creds.valid and creds.refresh_token:
        creds.refresh(Request())
        token_file.write_text(creds.to_json(), encoding="utf-8")
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def verify(account: dict | None = None):
    try:
        yt = _service(account)
        ch = yt.channels().list(part="snippet", mine=True).execute()
        items = ch.get("items", [])
        if not items:
            return False, "канал не найден"
        return True, items[0]["snippet"]["title"]
    except Exception as e:  # noqa: BLE001
        return False, str(e)


# категория YouTube по нише (27=Образование, 28=Наука/Техника, 24=Развлечения).
# Донорские RU-ниши оставлены для совместимости; EN-ниши этой фабрики — history_*.
# Приоритетнее карты — поле "category" самой ниши в niches.json (data-driven).
_YT_CAT = {"history_docs": "27", "history_shorts": "27",
           "ai_lifehacks": "28", "ai_lifehacks_en": "28", "personal_brand": "28",
           "mind_facts": "27", "money_facts": "27", "history_facts": "27",
           "business_stories": "27", "soviet_things": "27", "psy_stories": "27",
           "talking_objects": "24", "mystic_stories": "24", "what_if": "24"}


def _niche_of(meta: dict) -> dict:
    """Ниша из niches.json по meta['niche']; {} при любом сбое (ниша не критична)."""
    nid = meta.get("niche")
    if nid:
        try:
            return core.get_niche(nid) or {}
        except Exception:  # noqa: BLE001
            pass
    return {}


def _resolve_is_short(meta: dict, niche: dict, is_short: bool | None) -> bool:
    """Контракт: явный is_short → meta['format'] → format ниши → core.ACTIVE_FORMAT."""
    if is_short is not None:
        return bool(is_short)
    f = str(meta.get("format") or niche.get("format") or "").lower()
    if f:
        return f == "short"
    return core.ACTIVE_FORMAT == "short"


def _rfc3339_utc(when) -> str:
    """datetime|ISO-строка → RFC3339 UTC ('...Z'). Наивный datetime трактуем как core.TZ."""
    if isinstance(when, str):
        when = dt.datetime.fromisoformat(when.replace("Z", "+00:00"))
    if when.tzinfo is None:
        when = when.replace(tzinfo=core.TZ)
    return when.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_body(meta: dict, is_short: bool | None = None, publish_at=None,
               language: str | None = None) -> tuple[dict, bool]:
    """Собрать тело videos().insert (отделено от сети — тестируется оффлайн).
    Возвращает (body, is_short_resolved). "#Shorts" — только для shorts; publish_at →
    private + status.publishAt; язык: параметр → meta['lang'] → lang ниши → "en"."""
    niche = _niche_of(meta)
    short = _resolve_is_short(meta, niche, is_short)
    cap = meta.get("captions", {}).get("youtube", {})
    title = cap.get("title") or meta.get("topic", "Video")
    description = cap.get("description", "")
    if short and "#shorts" not in description.lower():
        # режем ДО добавления тега, чтобы "#Shorts" не отрезался у длинных описаний
        description = (description[:4870] + "\n\n#Shorts").strip()
    description = description[:4900]
    tags = [t.lstrip("#") for t in meta.get("hashtags", [])][:15]
    lang = (language or meta.get("lang") or niche.get("lang") or "en")
    cat = str(niche.get("category") or _YT_CAT.get(meta.get("niche"), "27"))
    body = {
        "snippet": {"title": title[:100], "description": description,
                    "tags": tags, "categoryId": cat,
                    "defaultLanguage": lang, "defaultAudioLanguage": lang},
        "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False},
    }
    if publish_at:
        body["status"]["privacyStatus"] = "private"   # требование API для publishAt
        body["status"]["publishAt"] = _rfc3339_utc(publish_at)
    return body, short


def publish(video_path: str, meta: dict, account: dict | None = None,
            is_short: bool | None = None, publish_at=None, language: str | None = None):
    """Залить видео. is_short/publish_at/language — см. build_body. Возвращает
    (True, {id, url, privacy[, publish_at]}); исключения пробрасываются (как у донора)."""
    _, MediaFileUpload, _, _ = _imports()
    yt = _service(account)
    body, short = build_body(meta, is_short=is_short, publish_at=publish_at, language=language)
    media = MediaFileUpload(video_path, chunksize=CHUNK, resumable=True, mimetype="video/mp4")
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    resp = None
    while resp is None:
        _, resp = req.next_chunk(num_retries=5)  # либа сама делает exp-backoff на 5xx/socket
    vid = resp["id"]
    privacy = resp.get("status", {}).get("privacyStatus", "?")
    # Кастомная обложка — главный CTR-рычаг. scope youtube.upload позволяет thumbnails().set
    # на своём только что загруженном видео. НИКОГДА не валим публикацию: видео уже загружено.
    thumb_path = meta.get("thumbnail")
    if thumb_path and os.path.exists(thumb_path):
        try:
            yt.thumbnails().set(
                videoId=vid,
                media_body=MediaFileUpload(thumb_path, mimetype="image/jpeg"),
            ).execute()
        except Exception as e:  # noqa: BLE001
            core.log_error("youtube.thumbnail", e, vid=vid)
    url = f"https://youtube.com/shorts/{vid}" if short else f"https://youtube.com/watch?v={vid}"
    out = {"id": vid, "url": url, "privacy": privacy}
    if publish_at:
        out["publish_at"] = body["status"]["publishAt"]
    return True, out


def upload_caption(video_id: str, srt_path: str | pathlib.Path,
                   language: str = "en", name: str = "English",
                   account: dict | None = None) -> bool:
    """Залить субтитры (.srt) к видео через captions().insert. НЕ-фатально, как обложки:
    любой сбой (нет force-ssl scope в токене, квота, сеть) → False + core.log_error.
    Вызывать ПОСЛЕ publish() с полученным video_id."""
    try:
        _, MediaFileUpload, _, _ = _imports()
        srt_path = pathlib.Path(srt_path)
        if not srt_path.exists():
            core.log_error("youtube.caption", FileNotFoundError(str(srt_path)), vid=video_id)
            return False
        yt = _service(account)
        body = {"snippet": {"videoId": video_id, "language": language,
                            "name": name, "isDraft": False}}
        media = MediaFileUpload(str(srt_path), mimetype="application/octet-stream")
        yt.captions().insert(part="snippet", body=body, media_body=media).execute()
        return True
    except Exception as e:  # noqa: BLE001
        core.log_error("youtube.caption", e, vid=video_id, srt=str(srt_path))
        return False


if __name__ == "__main__":
    core.load_local_secrets()
    ok, msg = verify()
    print(("✓ YouTube: " if ok else "✗ YouTube: ") + str(msg))
