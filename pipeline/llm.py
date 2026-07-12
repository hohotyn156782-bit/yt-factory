"""Мульти-провайдерный + мульти-ключевой LLM-слой с каскадным фолбэком.

Все провайдеры OpenAI-совместимы (POST {base}/chat/completions, Bearer key). Список и порядок —
в `llm_providers.json` (приоритет сверху вниз). У каждого провайдера может быть НЕСКОЛЬКО ключей
(разные аккаунты) — в env через запятую: GEMINI_API_KEY="key1,key2,key3". Кончилась квота у одного
ключа (429) → cooldown ЭТОГО ключа → пробуем следующий ключ → следующий провайдер. Система не падает.

Использование: llm.chat(system, user, json_mode=True, max_tokens=900, temp=0.85) -> str
"""
import os
import json
import time
import hashlib
import pathlib
import urllib.request
import urllib.error

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG = ROOT / "llm_providers.json"

import sys
sys.path.insert(0, str(ROOT))
import core  # noqa: E402

_cooldown: dict[str, float] = {}   # "provider:<sha1[:10]>" -> unix-ts, до которого ключ пропускаем
_last_used: dict[str, float] = {}

# Подтягиваем персист cooldown один раз при импорте модуля (короткоживущий cron
# иначе долбит уже исчерпанные 429-ключи заново). Ключи на диске непрозрачные (sha1).
try:
    _cooldown.update(core.load_cooldown("llm"))
except Exception:  # noqa: BLE001
    pass


class _QuotaError(Exception):
    """Исчерпана квота / 429 — ключ в cooldown (длина из Retry-After, иначе из конфига)."""
    def __init__(self, msg, retry_after=None):
        super().__init__(msg)
        self.retry_after = retry_after


class _TransientError(Exception):
    """Временная ошибка (5xx/таймаут/сеть) — короткий cooldown."""


def providers() -> list[dict]:
    if not CONFIG.exists():
        return []
    return [p for p in json.loads(CONFIG.read_text(encoding="utf-8")).get("providers", []) if p.get("enabled", True)]


def _keys_for(p: dict) -> list[str]:
    """Ключи провайдера: из key_env (можно несколько через запятую) + key_envs[]. Уникальные, по порядку."""
    raw = []
    raw += (os.environ.get(p.get("key_env", ""), "") or "").split(",")
    for ev in p.get("key_envs", []):
        raw += (os.environ.get(ev, "") or "").split(",")
    out, seen = [], set()
    for k in (x.strip() for x in raw):
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _kid(name: str, key: str) -> str:
    """Непрозрачный id ключа: name + sha1(rawkey)[:10] (сырой секрет на диск НЕ попадает)."""
    kid = hashlib.sha1(key.encode()).hexdigest()[:10] if key else "nokey"
    return f"{name}:{kid}"


def configured() -> list[str]:
    """Имена провайдеров, у которых есть хотя бы один ключ."""
    return [p["name"] for p in providers() if _keys_for(p)]


def status() -> list[dict]:
    now = time.time()
    out = []
    for p in providers():
        keys = _keys_for(p)
        if not keys and p.get("no_key"):
            keys = [""]                       # без ключа, но рабочий (Pollinations)
        ready = sum(1 for k in keys if now >= _cooldown.get(_kid(p["name"], k), 0))
        state = "no_key" if not keys else ("ready" if ready else "cooldown")
        out.append({"name": p["name"], "keys": len(keys), "ready_keys": ready, "state": state})
    return out


def _call(p: dict, key: str, msgs: list[dict], json_mode: bool, max_tokens: int, temp: float) -> str:
    payload = {"model": p["model"], "temperature": temp, "max_tokens": max_tokens, "messages": msgs}
    if json_mode and p.get("json_mode", True):
        payload["response_format"] = {"type": "json_object"}
    url = p["base_url"].rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json", "User-Agent": "content-factory"}
    if key:                                   # no_key-провайдеры (Pollinations) идут без авторизации
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers)
    try:
        j = json.loads(urllib.request.urlopen(req, timeout=p.get("timeout", 60)).read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")[:300]
        low = body.lower()
        ra = e.headers.get("Retry-After") if e.headers else None
        ra = int(ra) if (ra and str(ra).isdigit()) else None
        if e.code in (429, 402, 403) or any(w in low for w in ("quota", "rate limit", "exhaust", "insufficient", "limit reached")):
            raise _QuotaError(f"{e.code}: {body[:110]}", retry_after=ra)
        if e.code >= 500:
            raise _TransientError(f"{e.code}")
        raise RuntimeError(f"HTTP {e.code}: {body[:110]}")
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise _TransientError(str(e)[:80])
    ch = (j.get("choices") or [{}])[0]
    content = (ch.get("message") or {}).get("content")
    if not content:  # «думающие» модели (Gemini 2.5 и др.) могут вернуть пустой текст, съев лимит на reasoning
        fr = ch.get("finish_reason") or "empty"
        raise _TransientError(f"пустой ответ (finish_reason={fr})")
    return content.strip()


def chat(system: str, user: str, json_mode: bool = True, max_tokens: int = 900,
         temp: float = 0.85, messages: list[dict] | None = None) -> str:
    msgs = messages or [{"role": "system", "content": system}, {"role": "user", "content": user}]
    provs = providers()
    if not provs:
        raise RuntimeError("Нет llm_providers.json")
    errors, now = [], time.time()
    for p in provs:
        keys = _keys_for(p)
        if not keys and p.get("no_key"):
            keys = [""]                       # провайдер без ключа (напр. Pollinations) — псевдо-ключ
        if not keys:
            continue
        for key in keys:
            kid = _kid(p["name"], key)
            if now < _cooldown.get(kid, 0):
                continue
            try:
                out = _call(p, key, msgs, json_mode, max_tokens, temp)
                _last_used[kid] = time.time()
                return out
            except _QuotaError as e:
                cd = min(e.retry_after or p.get("cooldown_sec", 1800), 86400)
                until = time.time() + cd
                _cooldown[kid] = until
                core.save_cooldown("llm", {kid: until})   # kid непрозрачен (sha1) — безопасно на диск
                errors.append(f"{kid}: квота → cooldown {cd}с")
            except _TransientError as e:
                until = time.time() + p.get("transient_cooldown_sec", 30)
                _cooldown[kid] = until
                core.save_cooldown("llm", {kid: until})
                errors.append(f"{kid}: временно ({e})")
            except Exception as e:  # noqa: BLE001
                errors.append(f"{kid}: {str(e)[:80]}")
    raise RuntimeError("Все LLM (провайдеры×ключи) недоступны:\n  " + "\n  ".join(errors))


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(ROOT))
    import core
    core.load_local_secrets()
    print("Статус:", [(s["name"], s["state"], f"{s['ready_keys']}/{s['keys']} ключей") for s in status()])
    print("Активны:", configured())
    if configured():
        print("Тест:", chat("Отвечай JSON.", 'Верни {"ok": true}.', max_tokens=20))
