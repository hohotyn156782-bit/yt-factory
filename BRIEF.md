# Бриф: YT Factory — автономный контент-завод для US YouTube

## Задача (что сделать)
Работающий автономный завод: генерирует и публикует на новый US-YouTube-канал длинные документальные видео 10–12 мин (16:9, EN) и вертикальные Shorts, на GitHub Actions, ≤$30/мес (старт $0). Цель бизнеса — пройти YPP (1000 подп + 4000 часов) и зарабатывать с ad-монетизации.

## Зачем
Пассивный доход владельца с минимальным участием. Темы владельцу безразличны — выбраны по RPM/риску.

## Решения (по ресёрчу 2026-07-12, отчёты в docs/research/)
- **Ниша:** историческая документалистика, уклон wealth/business/engineering history (RPM $8–14, PD-визуалы, ноль defamation-риска, LLM пишет жанр отлично). НЕ клонировать «sleep history slop» (перегрет, мишень inauthentic-политики): выраженный editorial angle, главы, вариативный монтаж, источники в описании. Канал #2 (space/science, NASA PD) — позже на том же пайплайне.
- **Форматы:** long-form 10–12 мин (2–3/нед) + Shorts (~1/день) на ОДНОМ канале, каждый Short линкуется Related Link на тематически близкое длинное.
- **TTS:** Kokoro-82M ONNX (Apache 2.0, $0, RTF~0.5 на 4-core CI; голоса af_heart/am_michael). Фолбэк edge-tts EN. Апгрейд-опция: gpt-4o-mini-tts ~$6/мес (нужна карта).
- **Визуал long-form:** слоты 8–15с: Pexels/Pixabay landscape + Internet Archive/Prelinger PD + FLUX-картинки (NVIDIA ключ) с Ken Burns; смена «чего-то» каждые 3–5с (панорамы/punch-in без смены ассета). Позже: fal.ai i2v клипы (кредиты без карты) / Modal $30 free credits.
- **Субтитры:** long-form — SRT через captions API (не вжигать); Shorts — прежний pop-in стиль.
- **Публикация:** adapters/youtube.py существует и работает. День 1: GCP-проект → OAuth external → publish "In production" unverified (нет 7-дневной смерти refresh-токена) → аудит-форма API сразу. До аудита НЕ грузить через свой API (hard-lock в private) — мост: TG-очередь → ручная выкладка в Studio (2 мин/день). После аудита: videos.insert (1 юнит, лимит 100/день) + thumbnails.set + publishAt.
- **Анти-слоп (YPP):** вариативность длины/хуков/структуры между видео; главы с curiosity-заголовками; описание 150+ слов с источниками; AI-disclosure чекбокс для фотореалистичных AI-кадров; human-touch = QA-гейт + Gemini-vision.
- **Архитектура:** порт модулей content-factory по вердиктам docs/research/code-recon.md. Copy-as-is: llm, topics_db, parser, youtube_auth, core (почти), voice (почти). Copy-with-edits: broll, assemble, qa, imagegen, subtitles, selector, youtube, factory, workflows. Rewrite: script long-form, build long-form, autopilot, thumbnail 16:9.

## Границы (scope)
- Трогать: только `~/projects/yt-factory` (новый код) + новый GitHub-репо.
- НЕ трогать: `~/projects/content-factory` (боевой RU-прод) — read-only донор.
- Тяжёлые данные — не на C: (DATA_ROOT на D: локально, state/cfdata на CI).

## Критерий приёмки («готово»)
1. Локально: `factory.py build --format long` собирает 10–12-мин 1920×1080 EN-видео (Kokoro-озвучка, b-roll, главы, обложка 1280×720, SRT, QA ok) — файл существует и проигрывается.
2. Локально: `factory.py build --format short` собирает EN Short 1080×1920.
3. GitHub Actions: workflow-ран проходит на CI (dry-run без публикации).
4. Инструкция владельцу (аккаунт/канал/OAuth/аудит) написана.
5. Verifier-субагент подтверждает 1–3.

## Как проверять
Локальная сборка end-to-end + ffprobe + просмотр кадров; CI-ран через workflow_dispatch; verifier с этим брифом.

## Известное (не переоткрывать)
- Все грабли CI и вердикты модулей — docs/research/code-recon.md, progress.md.
- Ключи: `~/.config/content-factory/secrets.env` (наследуем паттерн), GitHub secret SECRETS_ENV.
- yt-dlp на CI виснет (бан IP GitHub) — отключать.
- YT скрытый лимит ~7-20 аплоадов/день/канал поверх квоты API.
