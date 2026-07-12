# YT Factory

Автономный контент-завод для US-YouTube-канала **Fortunes & Empires**: длинные исторические
документалки 10–12 мин (16:9, EN) + вертикальные Shorts. Работает на GitHub Actions, стоимость
инфраструктуры $0 (免 free-квоты LLM/стока/TTS). Цель — YPP-монетизация (1000 подп + 4000 часов).

Родословная: модули портированы из боевого `content-factory` (RU-shorts) с адаптацией под
EN/16:9/long-form. Решения и источники — `BRIEF.md` + `docs/research/*.md`. Состояние — `progress.md`.

## Пайплайн

```
selector (topic_bank + тренды) → script_long (outline → главы → polish → судья ≥70)
  → Kokoro-82M TTS (CPU, RTF~0.3) → Groq Whisper (пословные тайминги)
  → b-roll: Pexels/Pixabay landscape + FLUX 16:9 (NVIDIA) + Ken Burns, слоты 6-14с
  → ffmpeg 1920×1080 (loudnorm, дакинг музыки) → SRT (не вжигается)
  → QA-гейт (тех + Gemini-vision) → обложка 1280×720 → публикация
```

Shorts — тот же конвейер в вертикальном формате с popin-субтитрами (донорский путь).

## Команды

```bash
python3 factory.py doctor            # проверка окружения
python3 factory.py build history_docs "The Medici: ..."   # одно видео
python3 factory.py run long          # автопилот: документалка
python3 factory.py run shorts        # автопилот: шортс
```

## Публикация — два режима

1. **Мост (сейчас, до аудита YouTube API):** карточка в TG владельцу (title/description/теги/обложка
   + флаг AI-disclosure), файл видео — артефакт CI-рана. Выкладка руками в Studio ~2 мин.
2. **Авто (`YT_AUTO=1` в secrets):** прямой `videos.insert` + SRT-субтитры + обложка.
   ⚠️ Включать ТОЛЬКО после прохождения аудита Google — иначе видео навсегда блокируются в private
   (`docs/research/upload-api.md`). Заявка на аудит: см. `docs/SETUP_OWNER.md`.

## Секреты

Локально: `~/.config/yt-factory/secrets.env` (наследует ключи `content-factory`/`content-engine`).
CI: GitHub secret `SECRETS_ENV` = содержимое env-файла целиком. Ключевые переменные:
LLM-каскад (`GEMINI_API_KEY`, `MISTRAL_API_KEY`, `GROQ_API_KEY`, …), сток (`PEXELS_API_KEY`,
`PIXABAY_API_KEY`), картинки (`NVIDIA_API_KEY`), алерты (`TG_BOT_TOKEN`/`TG_CHAT_ID`),
мост (`TG_QUEUE_BOT_TOKEN`, `TG_QUEUE_CHAT_YOUTUBE`), автопостинг (`YT_AUTO`, `YT_CLIENT_SECRET_FILE`,
`YT_TOKEN_FILE`).

## Анти-слоп (политика YouTube «inauthentic content», июль 2025)

Вшито в генератор: детерминированная вариативность структуры между видео (5-7 глав, 4 формулы хука,
ритм, recap-глава), open-loop в конце каждой главы, конкретные факты/даты/имена, реальные книги
в «Further reading», главы-таймкоды, запрет клише-филлеров, AI-disclosure флаг при FLUX-кадрах.
