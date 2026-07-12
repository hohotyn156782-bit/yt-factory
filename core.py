"""Ядро Content Factory: пути, секреты, реестр ниш/каналов, история тем, утилиты.

Философия повторяет content-engine: секреты только через env, история append-only.
  - Локально:  ~/.config/content-factory/secrets.env  (+ наследуем ~/.config/content-engine/secrets.env)
  - В CI:      переменные приходят из repo Secrets

Видео = новый тип контента поверх той же дисциплины: антидубль тем, ротация ниш,
история произведённого и опубликованного.
"""
import os
import re
import gzip
import json
import time
import fcntl
import shlex
import pathlib
import logging
import logging.handlers
import threading
import subprocess
import datetime as dt
import urllib.request
import urllib.error

ROOT = pathlib.Path(__file__).resolve().parent
NICHES_FILE = ROOT / "niches.json"
# ВСЕ тяжёлые данные — на диск D (диск C переполнен, WSL живёт на C → писать туда нельзя).
# Если /mnt/d недоступен (другая машина/CI) — падаем обратно в папку проекта.
_D = pathlib.Path("/mnt/d/yt-factory-data")
# CF_DATA_ROOT (env) имеет приоритет — в CI указываем на коммитимую папку (state/cfdata),
# чтобы history.jsonl/cooldown переживали эфемерный раннер и работал антидубль тем между выходами дня.
_env_root = os.environ.get("CF_DATA_ROOT", "").strip()
if _env_root:
    DATA_ROOT = pathlib.Path(_env_root)
elif pathlib.Path("/mnt/d").is_dir():
    DATA_ROOT = _D
else:
    DATA_ROOT = ROOT
try:
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
except Exception:  # noqa: BLE001
    pass
HISTORY_FILE = DATA_ROOT / "history.jsonl"

OUTPUT_DIR = DATA_ROOT / "output"
CACHE_DIR = DATA_ROOT / "cache"
ASSETS_DIR = ROOT / "assets"          # входные ассеты (sfx) — мелкие, остаются с кодом
MUSIC_DIR = DATA_ROOT / "music"       # музыка может быть тяжёлой → на D
MEDIA_DIR = DATA_ROOT / "media_assets"  # материалы для сборки (картинки/фото каждого ролика)
PUBLISH_DIR = DATA_ROOT / "publish"   # готовые видео для публикации
LOGS_DIR = DATA_ROOT / "logs"         # журнал событий/ошибок (JSON-строки)
HEARTBEAT_FILE = LOGS_DIR / "heartbeat.json"
SCHEDULER_LOCK = ROOT / "scheduler.lock"   # ext4 (не /mnt/d NTFS) — flock надёжен

MIN_FREE_MB = 1500    # ниже этого порога свободного места сборку не начинаем (анти-«диск переполнен»)
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"

# Секреты: сначала свои, потом наследуем LLM/TG-ключи от content-factory/engine (setdefault — не перетираем).
SECRET_FILES = [
    pathlib.Path("~/.config/yt-factory/secrets.env").expanduser(),
    pathlib.Path("~/.config/content-factory/secrets.env").expanduser(),
    pathlib.Path("~/.config/content-engine/secrets.env").expanduser(),
]

from zoneinfo import ZoneInfo  # noqa: E402
TZ = ZoneInfo("America/New_York")  # таргет-аудитория US: расписание и история в ET

# Жирный шрифт с кириллицей и латиницей (проверен в системе).
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_FAMILY = "DejaVu Sans"

# Два формата: long = горизонтальный YouTube (16:9), short = вертикальный Shorts (9:16).
# Модули читают core.W/core.H через атрибут → переключение set_format() видно везде;
# module-level производные (assemble) обязаны пересчитываться на каждый вызов, не при импорте.
FORMATS = {"long": (1920, 1080), "short": (1080, 1920)}
FPS = 30
W, H = FORMATS["long"]
ACTIVE_FORMAT = "long"


def set_format(fmt: str) -> None:
    global W, H, ACTIVE_FORMAT
    W, H = FORMATS[fmt]
    ACTIVE_FORMAT = fmt

# XTTS живёт в изолированном venv (системный Python с боевыми ботами не трогаем).
# Движок озвучки engine="xtts" вызывает воркер этим интерпретатором как подпроцесс.
XTTS_VENV_PY = ROOT / ".venv-xtts" / "bin" / "python"
XTTS_WORKER = ROOT / "pipeline" / "xtts_worker.py"
# MOSS-TTS-Nano — свой venv: локальный CPU-TTS + клон голоса из 6-сек сэмпла (разнообразие голосов).
MOSS_VENV_PY = ROOT / ".venv-moss" / "bin" / "python"
MOSS_WORKER = ROOT / "pipeline" / "moss_worker.py"


# ──────────────────────────── Секреты ────────────────────────────

_SECRETS: set[str] = set()   # значения секретов для маскировки в логах (_scrub)
_SECRET_NAME_RE = re.compile(r"(?i)(token|secret|key|password)")


def load_local_secrets() -> None:
    for f in SECRET_FILES:
        if not f.exists():
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            os.environ.setdefault(k, v)
            if v and len(v) > 8 and _SECRET_NAME_RE.search(k):
                _SECRETS.add(v)


def secret(name: str, required: bool = True) -> str:
    val = os.environ.get(name, "")
    if required and not val:
        raise RuntimeError(f"Нет секрета {name} (локально: secrets.env, в CI: GitHub Secrets)")
    if val and len(val) > 8 and _SECRET_NAME_RE.search(name):
        _SECRETS.add(val)
    return val


def has_secret(name: str) -> bool:
    return bool(os.environ.get(name, ""))


def _scrub(s: str) -> str:
    """Заменить любое известное значение секрета на *** в строке (анти-утечка в логи)."""
    if not s or not isinstance(s, str):
        return s
    for v in _SECRETS:
        if v:
            s = s.replace(v, "***")
    return s


# ──────────────────────────── Ниши ────────────────────────────

def load_niches(only_enabled: bool = True) -> list[dict]:
    data = json.loads(NICHES_FILE.read_text(encoding="utf-8"))
    niches = data.get("niches", [])
    if only_enabled:
        niches = [n for n in niches if n.get("enabled", True)]
    return niches


def get_niche(niche_id: str) -> dict:
    n = next((x for x in load_niches(only_enabled=False) if x["id"] == niche_id), None)
    if not n:
        raise RuntimeError(f"Нет ниши {niche_id} в niches.json")
    return n


# ──────────────────────────── Время / история ────────────────────────────

def _now() -> dt.datetime:
    iso = os.environ.get("CF_NOW")
    if iso:
        return dt.datetime.fromisoformat(iso).astimezone(TZ)
    return dt.datetime.now(dt.timezone.utc).astimezone(TZ)


def stamp() -> str:
    return _now().strftime("%Y%m%d-%H%M%S")


def today_str() -> str:
    return _now().strftime("%Y-%m-%d")


def load_history() -> list[dict]:
    if not HISTORY_FILE.exists():
        return []
    out = []
    for line in HISTORY_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


_history_lock = threading.RLock()   # панель пишет историю из фоновых потоков → защищаем


def rotate_log(path: pathlib.Path, max_mb: int = 50, keep: int = 3) -> None:
    """Если файл-журнал перерос max_mb — переименовать в датированный бэкап (анти-бесконечный рост).
    Держим последние `keep` бэкапов, старые удаляем."""
    try:
        if not path.exists() or path.stat().st_size < max_mb * 1024 * 1024:
            return
        bak = path.with_name(f"{path.stem}-{stamp()}{path.suffix}.bak")
        path.rename(bak)
        backups = sorted(path.parent.glob(f"{path.stem}-*{path.suffix}.bak"))
        for old in backups[:-keep]:
            old.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001 — ротация не должна ронять пайплайн
        pass


def append_history(entry: dict) -> None:
    entry = {"ts": _now().isoformat(), **entry}
    with _history_lock:
        rotate_log(HISTORY_FILE, max_mb=50)
        with HISTORY_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())   # гарантия записи на диск (cron может умереть сразу после)


def recent_topics(niche_id: str, days: int = 30, history: list[dict] | None = None) -> list[str]:
    """Темы видео этой ниши за последние `days` дней — чтобы Groq не повторялся."""
    history = history if history is not None else load_history()
    cutoff = (_now() - dt.timedelta(days=days)).isoformat()
    return [e.get("topic", "") for e in history
            if e.get("niche") == niche_id and e.get("topic")
            and str(e.get("ts", "")) >= cutoff]


# ──────────────────────────── Утилиты ────────────────────────────

_INJECT = re.compile(r"(?i)(ignore|disregard|forget)\s+(all\s+)?(previous|above|prior|the)\b.{0,40}|(system\s+prompt)|(you\s+are\s+now)|(<\|.*?\|>)|(```)")


def sanitize_external(t: str) -> str:
    """Очистить любой внешний текст (тренды/topic/heatmap) перед вставкой в LLM-промпт."""
    if not t:
        return t
    t = _INJECT.sub(" ", str(t))
    t = re.sub(r"[`{}<>\\]", "", t)
    t = re.sub(r"\s+", " ", t).strip().strip('"').strip("'")
    return t[:140]


def slugify(text: str, maxlen: int = 40) -> str:
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE).strip().lower()
    text = re.sub(r"[\s_-]+", "-", text)
    # транслит кириллицы для безопасных путей (path-guard: только ascii в путях)
    table = {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
        "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
        "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts",
        "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
        "я": "ya",
    }
    text = "".join(table.get(c, c) for c in text)
    text = re.sub(r"[^a-z0-9-]", "", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return (text or "video")[:maxlen]


def run(cmd: list[str], quiet: bool = True, timeout: int = 600) -> subprocess.CompletedProcess:
    """Запустить команду; при ненулевом коде кинуть с хвостом stderr."""
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-1200:]
        raise RuntimeError(f"Команда упала ({proc.returncode}): {' '.join(shlex.quote(c) for c in cmd[:6])}…\n{tail}")
    return proc


def run_retry(cmd: list[str], attempts: int = 2, pause: float = 3.0, **kw) -> subprocess.CompletedProcess:
    """run() с повтором — для ИДЕМПОТЕНТНЫХ ffmpeg-команд с -y (транзиентный сбой не роняет сборку)."""
    last = None
    for i in range(attempts):
        try:
            return run(cmd, **kw)
        except Exception as e:  # noqa: BLE001
            last = e
            if i < attempts - 1:
                log(f"команда упала (попытка {i + 1}/{attempts}), повтор через {pause}с", level="warn")
                time.sleep(pause)
    raise last


def media_duration(path: str | pathlib.Path) -> float:
    """Длительность медиафайла в секундах через ffprobe. 0.0 при отсутствии/битом/пустом файле."""
    p = pathlib.Path(path)
    if not p.exists() or p.stat().st_size == 0:
        return 0.0
    try:
        proc = run([
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(p),
        ])
    except RuntimeError:
        return 0.0
    try:
        return float(proc.stdout.strip())
    except (ValueError, AttributeError):
        return 0.0


def ensure_dirs() -> None:
    for d in (OUTPUT_DIR, CACHE_DIR, ASSETS_DIR, MUSIC_DIR, MEDIA_DIR, PUBLISH_DIR, LOGS_DIR):
        d.mkdir(parents=True, exist_ok=True)
    # Одноразовая миграция history.jsonl: ROOT (старое место) → DATA_ROOT (диск D).
    old_hist = ROOT / "history.jsonl"
    if old_hist.exists() and not HISTORY_FILE.exists():
        try:
            HISTORY_FILE.write_bytes(old_hist.read_bytes())
        except Exception:
            pass


# ──────────────────────────── Логирование (видимость сбоев) ────────────────────────────

def _setup_logger() -> logging.Logger:
    """Логгер с посуточной ротацией (TimedRotatingFileHandler), сжатием бэкапов в .gz, хранением 30 дней."""
    lg = logging.getLogger("content_factory")
    lg.setLevel(logging.INFO)
    lg.propagate = False
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = str(LOGS_DIR / "factory.log")
    # защита от дублей хендлеров (модуль может импортироваться повторно)
    for h in lg.handlers:
        if isinstance(h, logging.handlers.TimedRotatingFileHandler) and getattr(h, "baseFilename", "") == os.path.abspath(log_path):
            return lg
    handler = logging.handlers.TimedRotatingFileHandler(
        log_path, when="midnight", backupCount=30, encoding="utf-8", delay=True,
    )

    def _namer(name: str) -> str:
        return name + ".gz"

    def _rotator(source: str, dest: str) -> None:
        try:
            with open(source, "rb") as sf, gzip.open(dest, "wb") as df:
                df.writelines(sf)
            os.remove(source)
        except Exception:  # noqa: BLE001 — ротация не должна ронять пайплайн
            pass

    handler.namer = _namer
    handler.rotator = _rotator
    handler.setFormatter(logging.Formatter("%(message)s"))
    lg.addHandler(handler)
    return lg


_logger = _setup_logger()


def log(msg: str, level: str = "info", **fields) -> None:
    """Единый журнал: строка JSON в logs/factory.log + печать. Чтобы сбои не были «тихими».
    Секреты маскируются (_scrub) и в записи, и в выводе."""
    msg = _scrub(msg)
    fields = {k: (_scrub(v) if isinstance(v, str) else v) for k, v in fields.items()}
    rec = {"ts": _now().isoformat(), "level": level, "msg": msg, **fields}
    try:
        _logger.info(json.dumps(rec, ensure_ascii=False))
    except Exception:  # noqa: BLE001 — журнал не должен ронять пайплайн
        pass
    icon = {"error": "❌", "warn": "⚠️ ", "info": "·"}.get(level, "·")
    print(f"{icon} {msg}" + (f"  {fields}" if fields else ""))


def log_error(where: str, exc: Exception, **fields) -> None:
    """Залогировать пойманное исключение (вместо тихого глотания)."""
    log(f"{where}: {type(exc).__name__}: {str(exc)[:300]}", level="error", where=where, **fields)


# ──────────────────────────── HTTP с повторами ────────────────────────────

def _safe_url(url: str) -> str:
    """Замаскировать секреты в URL перед логированием (key/token/access_token/api_key/secret=...).
    Дополнительно прячет токен в ПУТИ Telegram (/bot<token>/)."""
    url = re.sub(r"/bot\d+:[A-Za-z0-9_-]+", "/bot***", str(url))
    return re.sub(r"((?:access_token|api_?key|key|token|secret|client_secret)=)[^&\s]+",
                  r"\1***", url, flags=re.IGNORECASE)[:80]


def http_json(url: str, headers: dict | None = None, timeout: int = 30,
              retries: int = 3, data: bytes | None = None) -> dict | None:
    """GET/POST JSON с экспоненциальным backoff. Возвращает dict или None (с логом).
    Сетевой блип/5xx больше не убивает ролик молча — будет повтор и запись в журнал."""
    h = {"User-Agent": UA, **(headers or {})}
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (400, 401, 403, 404):   # клиентские — повтор не поможет
                break
        except Exception as e:  # noqa: BLE001 — сеть/таймаут/парс → повтор
            last = e
        if attempt < retries - 1:
            time.sleep(1.5 * (2 ** attempt))     # 1.5s, 3s, 6s
    if last:
        log_error(f"http_json {_safe_url(url)}", last)
    return None


def http_download(url: str, dest: pathlib.Path, headers: dict | None = None,
                  timeout: int = 90, retries: int = 3, min_bytes: int = 5000) -> bool:
    """Скачать файл с повторами. True при успехе. Логирует провал вместо тихого None."""
    h = {"User-Agent": UA, **(headers or {})}
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
            if len(data) < min_bytes:
                last = RuntimeError(f"файл слишком мал ({len(data)}б)")
            else:
                dest.write_bytes(data)
                return True
        except Exception as e:  # noqa: BLE001
            last = e
        if attempt < retries - 1:
            time.sleep(1.5 * (2 ** attempt))
    if last:
        log_error(f"download {_safe_url(url)}", last)
    return False


# ──────────────────────────── Диск ────────────────────────────

def free_space_mb(path: pathlib.Path | None = None) -> int:
    """Свободно МБ на диске с данными (по умолчанию DATA_ROOT)."""
    import shutil as _sh
    target = path or DATA_ROOT
    try:
        return int(_sh.disk_usage(target).free / (1024 * 1024))
    except Exception:  # noqa: BLE001
        return 10 ** 9   # не смогли измерить → не блокируем


def check_disk(min_mb: int = MIN_FREE_MB) -> None:
    """Бросить понятную ошибку, если на диске данных мало места (анти-«молчаливый ffmpeg-крах»)."""
    free = free_space_mb()
    if free < min_mb:
        raise RuntimeError(f"Мало места на диске ({free} МБ < {min_mb} МБ) — {DATA_ROOT}. "
                           f"Освободи место или смени DATA_ROOT.")


def cleanup_cache(max_age_days: int = 7) -> int:
    """Удалить из CACHE_DIR файлы старше N дней (скачанный сток копится). Возвращает сколько удалил."""
    if not CACHE_DIR.exists():
        return 0
    cutoff = time.time() - max_age_days * 86400
    n = 0
    try:
        for f in CACHE_DIR.iterdir():
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
                n += 1
    except Exception as e:  # noqa: BLE001
        log_error("cleanup_cache", e)
    if n:
        log(f"кэш очищен: удалено {n} старых файлов", level="info")
    return n


def cleanup_outputs(max_age_days: int = 14) -> int:
    """Удалить старые рабочие папки роликов (OUTPUT_DIR) и опубликованные mp4 (PUBLISH_DIR) старше N дней —
    иначе диск растёт безгранично (cleanup_cache трогает только CACHE_DIR)."""
    import shutil as _sh
    cut = time.time() - max_age_days * 86400
    n = 0
    for d in (OUTPUT_DIR, PUBLISH_DIR):
        if not d.exists():
            continue
        for p in list(d.iterdir()):
            try:
                if p.stat().st_mtime < cut:
                    _sh.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)
                    n += 1
            except Exception:  # noqa: BLE001
                pass
    if n:
        log(f"очищено старых output/publish: {n}", level="info")
    return n


def cleanup_media(max_age_days: int = 45) -> int:
    """Удалить папки материалов роликов (MEDIA_DIR) старше N дней (каталог для повторного монтажа → срок больше outputs)."""
    import shutil as _sh
    if not MEDIA_DIR.exists():
        return 0
    cut = time.time() - max_age_days * 86400
    n = 0
    for p in list(MEDIA_DIR.iterdir()):
        try:
            if p.stat().st_mtime < cut:
                _sh.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)
                n += 1
        except Exception:
            pass
    if n:
        log(f"очищено старых media_assets: {n}", level="info")
    return n


# ──────────────────────────── Межпроцессный flock (анти-перекрытие cron) ────────────────────────────

def acquire_lock(path: "pathlib.Path | None" = None):
    """Захватить эксклюзивный неблокирующий flock. None если уже занят (предыдущий запуск жив)."""
    fh = open(path or SCHEDULER_LOCK, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh
    except BlockingIOError:
        fh.close()
        return None


def release_lock(fh) -> None:
    try:
        if fh:
            fcntl.flock(fh, fcntl.LOCK_UN)
            fh.close()
    except Exception:
        pass


# ──────────────────────────── Персист cooldown между cron-запусками ────────────────────────────

def load_cooldown(name: str) -> dict:
    """Прочитать карту cooldown {opaque_id: epoch_until}, отбросив истёкшие. Ключи ДОЛЖНЫ быть непрозрачными (вызывающий хеширует секреты сам)."""
    p = CACHE_DIR / f"cooldown_{name}.json"
    try:
        if p.exists():
            now = time.time()
            return {k: float(v) for k, v in json.loads(p.read_text(encoding="utf-8")).items() if float(v) > now}
    except Exception as e:
        log_error("load_cooldown", e)
    return {}


def save_cooldown(name: str, cd: dict) -> None:
    """Merge-on-write (переживает наложенные процессы) + атомарная запись tmp→replace."""
    p = CACHE_DIR / f"cooldown_{name}.json"
    try:
        now = time.time()
        merged = load_cooldown(name)
        for k, v in cd.items():
            v = float(v)
            if v > now:
                merged[k] = max(merged.get(k, 0.0), v)
        merged = {k: v for k, v in merged.items() if v > now}
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(merged), encoding="utf-8")
        tmp.replace(p)
    except Exception as e:
        log_error("save_cooldown", e)


# ──────────────────────────── Heartbeat (локальный dead-man's switch) ────────────────────────────

def safe_write(path: "pathlib.Path", data: bytes) -> None:
    """Атомарная запись через tmp→rename (читатель не видит полуфайл). НЕ для append-only."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.rename(path)


def beat(cmd: str) -> None:
    """Записать пульс {ts,cmd,pid} в HEARTBEAT_FILE (fallback /tmp если D недоступен)."""
    payload = json.dumps({"ts": _now().isoformat(), "cmd": cmd, "pid": os.getpid()}, ensure_ascii=False).encode("utf-8")
    for target in (HEARTBEAT_FILE, pathlib.Path("/tmp/cf-heartbeat.json")):
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            safe_write(target, payload)
            return
        except Exception:
            pass


def check_heartbeat(max_age_sec: int = 1800) -> dict:
    """Жив ли планировщик: возраст последнего пульса morning/tick."""
    for path in (HEARTBEAT_FILE, pathlib.Path("/tmp/cf-heartbeat.json")):
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            ts = dt.datetime.fromisoformat(data.get("ts", ""))
            age = int(_now().timestamp() - ts.timestamp())
            return {"alive": age < max_age_sec, "age_sec": age, "last_cmd": data.get("cmd", ""), "last_pid": data.get("pid"), "ts": data.get("ts", ""), "source": str(path)}
        except Exception:
            pass
    return {"alive": False, "age_sec": -1, "last_cmd": "", "last_pid": None, "ts": "", "source": ""}


# ──────────────────────────── Healthchecks (мониторинг НЕ-запуска) ────────────────────────────
# Главная дыра 24/7 на WSL-десктопе: если ПК спал / WSL не поднялся / cron не сработал — код НЕ
# запустился и потому НЕ пожалуется. Внешний dead-man's switch (healthchecks.io, free 20 проверок)
# ждёт периодический ping; не пришёл — сам шлёт алерт. Ставим ping в начало (start) и конец (success),
# в except — fail. URL берём из env per-slug: HC_PING_MORNING / HC_PING_TICK = базовый ping-URL чека.

def hc_ping(slug: str, kind: str = "success", timeout: int = 10) -> bool:
    """Пинг Healthchecks для чека `slug` (morning/tick): kind = start|success|fail.
    Базовый URL чека — в env HC_PING_<SLUG> (напр. HC_PING_MORNING). Нет URL → молча False
    (фича опциональна). Никогда не роняет пайплайн."""
    base = os.environ.get(f"HC_PING_{slug.upper()}", "").strip().rstrip("/")
    if not base:
        return False
    suffix = {"start": "/start", "fail": "/fail", "success": ""}.get(kind, "")
    try:
        urllib.request.urlopen(base + suffix, timeout=timeout)
        return True
    except Exception:  # noqa: BLE001 — мониторинг не должен ронять работу
        return False
