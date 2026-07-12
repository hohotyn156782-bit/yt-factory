# progress.md — YT Factory (американский YouTube, автономный контент-завод)

## Цель
Автономный завод видео для нового US-YouTube-канала: длинные видео 8–12 мин (16:9) + Shorts, генерация и публикация без участия владельца, бюджет ≤$30/мес (в идеале $0), цель — пройти YPP (1000 подп + 4000 часов) и зарабатывать на монетизации.

## Статус: в работе
Обновлено: 2026-07-12

## Решения и почему (чтобы не пересматривать заново)
- Формат: ОБА потока сразу (long-form + Shorts) — выбор пользователя 2026-07-12.
- Отдельный проект `~/projects/yt-factory` (не расширять content-factory) — RU-прод не трогаем, свой CI-бюджет. Выбор пользователя.
- Google-аккаунт: пользователь создаст НОВЫЙ под US-контент (я дам инструкцию). Выбор пользователя.
- Бюджет: до $30/мес. Выбор пользователя.
- Ниша/темы: пользователю безразличны → выбираю сам по RPM/риску (решение после ресёрча).
- Репо будет ПУБЛИЧНЫМ (как content-factory) — безлимитные минуты GitHub Actions (проверено: content-factory public).
- Переиспользуем проверенные модули content-factory (LLM-каскад, voice, broll, subtitles, assemble, QA, imagegen, topics_db) копированием с правками под EN/16:9/long-form.

## Сделано (только проверенное; с доказательством)
- [x] Интервью с пользователем (формат/архитектура/канал/бюджет) — ответы получены 2026-07-12.
- [x] Разведка content-factory — прочитаны карточки памяти + структура репо; YouTube-адаптер там ЕСТЬ и работает (adapters/youtube.py, resumable upload, multi-channel токены), OAuth просто не проходили.
- [x] Ресёрч: 8 агентов завершены (wf_c5c33d50-187), полные отчёты сохранены в `docs/research/*.md` (ypp-policy, upload-api, tts-english, niche, craft, code-recon, tools-catalog, budget-30).
- [x] Решения приняты и зафиксированы в BRIEF.md: ниша = историческая документалистика (wealth/business/engineering скос); TTS = Kokoro-82M; публикация = API после аудита Google (форма в день 1), до этого TG-очередь; Shorts+long на одном канале.
- [x] Память: карточка project_yt_factory.md записана.

## Сделано (порт кода, всё проверено прогонами 2026-07-12 вечер)
- [x] core.py: FORMATS long/short + set_format(), TZ America/New_York, секреты ~/.config/yt-factory (наследуют content-factory/engine) — проверено импортом и e2e-тестами ниже.
- [x] voice.py: движок Kokoro-82M (авто-скачивание модели 353МБ в кэш, RTF 0.30) — живой тест: 15с озвучка am_michael + Groq-выравнивание 36 слов.
- [x] broll/imagegen: landscape-ориентация всех источников + слот-планер long (71 слот/11 мин) + режим mixed (35% FLUX) + NVIDIA 1344×768 — живой Pexels 1920×1080 + 1 FLUX-кадр.
- [x] script.py EN + script_long.py (outline→главы→polish→судья; вариативность анти-слоп) — живая генерация: 1474 слова, 5 глав 240-306 слов, judge 88/100, validate ok (после моего фикса недобора слов: завышенный запрос в промпте + _expand_chapter).
- [x] assemble/subtitles/qa: рендер 16:9 проверен (1920×1080 h264+aac), build_srt корректный SRT, QA правильно валит синтетику (тишина/короткость/фризы) с long-окном 480-900с.
- [x] thumbnail 1280×720 + youtube.py (#Shorts условно, publishAt, captions, defaultLanguage) — обложка собрана 56КБ, 9 offline-ассертов build_body.
- [x] selector/parser/niches.json: 2 ниши (history_docs long + history_shorts), topic_bank 42 темы, живые 50 трендов (news/reddit/wiki, 0 VK/TG), 5 живых EN-тем от селектора.
- [x] build.py: диспатч по формату + _build_once_long (главы-таймкоды, AI-disclosure флаг, POST.txt для ручной выкладки) — компилируется, e2e идёт (см. ниже).
- [x] autopilot.py: post_youtube (YT_AUTO / TG-мост с артефактом CI), run long|shorts, кап 90 мин для long — компилируется+импортируется.
- [x] factory.py CLI, requirements.txt (+kokoro-onnx), .gitignore, README.md, .github/workflows/autopilot.yml (крон закомментирован до запуска).
- [x] docs/SETUP_OWNER.md — пошаговая инструкция владельцу (аккаунт/канал/OAuth/аудит-форма с заготовками ответов).

## В работе сейчас
- [ ] e2e-сборка первой документалки (фон, task b4njofal4): Rockefeller, запущена 21:22 МСК. По завершении: проверить mp4/SRT/обложку/QA глазами.

## Дальше (по порядку)
- [ ] Short e2e (быстрый, донорский путь)
- [ ] Верификатор по брифу
- [ ] GitHub repo (public) + секреты SECRETS_ENV + workflow_dispatch тест на CI
- [ ] Владелец: Части 1-3 из docs/SETUP_OWNER.md → OAuth → включить крон
- [ ] После аудита Google: YT_AUTO=1 (прямой автопостинг + publishAt)
- [ ] Позже: report.yml (ежедневная сводка, ждёт YOUTUBE_API_KEY), музыка в MUSIC_DIR, fal.ai i2v вставки ($24/мес опция), 2-й канал space

## Грабли и уроки этого проекта
- (из content-factory) yt-dlp виснет на CI (YouTube банит IP GitHub) — на CI отключать (`CF_NO_YTDLP`).
- (из content-factory) GitHub Actions джоб ≤2ч → бюджет времени на ран + per-niche таймаут обязательны.
- (из content-factory) FLUX нормально рисует ПРЕДМЕТЫ, людей/бренды — артефакты; QA-гейт обязателен.
- (из content-factory) Pollinations-фолбэк картинок отдаёт 576×1024 (мыло) при исчерпании NVIDIA 40/день.

## НЕ делать / вне scope
- Не трогать `~/projects/content-factory` (боевой RU-автопилот) — только читать/копировать.
- Никакого чужого контента/реаплоадов (Content ID, бан) — только оригинал: свой сценарий + лицензионный сток/PD/AI.
- Не класть тяжёлые данные на C: (диск переполнялся) — выводы рендеров на D: или в CI-артефакты.
