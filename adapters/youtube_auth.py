"""Одноразовая OAuth-авторизация YouTube. Запускать ЛОКАЛЬНО (нужен браузер).

Перед запуском:
  1. console.cloud.google.com → создать проект → включить YouTube Data API v3.
  2. OAuth consent screen: External + PUBLISH (Production), иначе токен живёт 7 дней.
  3. Credentials → OAuth client ID → тип Desktop app → скачать JSON.
  4. Положить путь в env: export YT_CLIENT_SECRET_FILE=/path/client_secret.json

Запуск (ОДИН канал, дефолт): python3 -m adapters.youtube_auth
  → токен в YT_TOKEN_FILE (по умолчанию ~/.config/content-factory/yt_token.json).

Запуск (СЕТЬ каналов): python3 adapters/youtube_auth.py <name>
  где <name> = secret_ref (или name) аккаунта канала из панели.
  → токен в core.DATA_ROOT/yt_tokens/<name>.json. Повторить для каждого канала сети.
  Войти в браузере под Google-аккаунтом нужного канала.

Затем скопировать токен(ы) на сервер/в CI-секрет.
"""
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import core  # noqa: E402

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def _default_token_file() -> pathlib.Path:
    return pathlib.Path(os.environ.get(
        "YT_TOKEN_FILE", str(pathlib.Path("~/.config/content-factory/yt_token.json").expanduser())))


def gen_token(name: str | None = None) -> pathlib.Path:
    """OAuth-флоу + сохранение токена. name задан → core.DATA_ROOT/yt_tokens/<name>.json
    (канал сети); name пуст → одноканальный YT_TOKEN_FILE (обратная совместимость)."""
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        raise SystemExit("Установи: pip install --break-system-packages google-auth-oauthlib")

    cs = os.environ.get("YT_CLIENT_SECRET_FILE", "")
    if not cs or not pathlib.Path(cs).exists():
        raise SystemExit("Нет YT_CLIENT_SECRET_FILE (путь к client_secret.json из GCP).")

    name = (name or "").strip()
    if name:
        token_file = core.DATA_ROOT / "yt_tokens" / f"{name}.json"
    else:
        token_file = _default_token_file()
    token_file.parent.mkdir(parents=True, exist_ok=True)

    flow = InstalledAppFlow.from_client_secrets_file(cs, SCOPES)
    creds = flow.run_local_server(port=0)  # откроет браузер; на headless используйте проброс порта
    token_file.write_text(creds.to_json(), encoding="utf-8")
    try:
        token_file.chmod(0o600)
    except OSError:
        pass
    print(f"✓ Токен сохранён: {token_file}")
    return token_file


def main():
    name = sys.argv[1] if len(sys.argv) > 1 else ""
    gen_token(name)


if __name__ == "__main__":
    main()
