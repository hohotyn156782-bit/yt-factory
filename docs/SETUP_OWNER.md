# Инструкция владельцу — что сделать руками (один раз)

Всё остальное автоматика. Здесь только то, что физически требует твоих рук/браузера.
По ресёрчу `docs/research/upload-api.md` — там источники на каждый пункт.

---

## Часть 1. Google-аккаунт и канал (~15 мин)

1. Создай **новый Google-аккаунт** (не основной): accounts.google.com → «Создать аккаунт» → «Для себя».
   - Имя любое нейтральное EN (например Alex Carter). Телефон подтвердить придётся (можно свой).
   - Включи **двухэтапную защиту** сразу (Настройки → Безопасность) — без неё YPP не пустят.
2. Зайди на youtube.com с этого аккаунта → аватар → «Создать канал».
   - **Название канала: `Fortunes & Empires`** (моё предложение под нишу «история богатства, бизнес-империй и инженерии»; можешь заменить — скажи мне, я поменяю в конфиге).
   - Handle: `@FortunesAndEmpires` (или свободный близкий).
3. YouTube Studio → Настройки → Канал:
   - Страна: честно Армения (RPM зависит от географии ЗРИТЕЛЕЙ, не канала — US-аудитория даст US-ставки).
   - Ключевые слова канала: history documentary, business history, engineering history.
4. Описание канала (вставь как есть):
   > Documentaries about the fortunes, empires and machines that built our world. How businesses rose and collapsed, how money actually worked in the past, and the engineering feats nobody talks about. New documentaries every week.
5. Studio → Настройки → Канал → «Расширенные настройки» → подтверди телефон (даёт кастомные обложки и видео >15 мин).

## Часть 2. Google Cloud + OAuth (~15 мин, критичные галочки!)

1. console.cloud.google.com (тем же новым аккаунтом) → создать проект, имя `yt-factory`.
2. «APIs & Services» → Library → включи **YouTube Data API v3**.
3. «APIs & Services» → OAuth consent screen:
   - User type: **External** → Create.
   - App name `yt-factory`, support email — этот же gmail. Сохраняй по шагам.
   - Scopes можно не добавлять на этом экране (запросим при авторизации).
   - Test users: добавь этот же gmail.
   - ⚠️ **САМОЕ ВАЖНОЕ:** после создания зайди в «Publishing status» и нажми **«Publish app»** → статус **In production** (останется «unverified» — это НОРМАЛЬНО и нам достаточно). Если оставить Testing — refresh-токен умирает каждые 7 дней и автоматика встанет.
4. «Credentials» → Create credentials → **OAuth client ID** → Application type: **Desktop app** → Create → **Download JSON**.
5. Файл положи в WSL как `~/.config/yt-factory/client_secret.json` (скажи мне когда готово — я запущу одноразовую авторизацию, ты кликнешь «разрешить» в браузере, и refresh-токен сохранится навсегда).

## Часть 3. Аудит YouTube API (подать в день 1, answer-заготовки ниже)

Без аудита ЛЮБОЕ видео, загруженное через наш API-проект, **навсегда блокируется в private** (разблокировать нельзя, только перезаливать руками). Поэтому:
- Форма: https://support.google.com/youtube/contact/yt_api_form
- Срок рассмотрения: 2–4 недели (бывает дольше). Пока ждём — публикуем вручную (Часть 5).

Что писать (мои заготовки, отвечай от себя):
- **Use case:** "Personal automation tool for my own YouTube channel. A private script renders my original documentary videos and uploads them to my own channel via videos.insert. Single user (me), single channel, no third-party access, no data collection. Default quota is sufficient."
- **API Client:** name `yt-factory`, internal script (GitHub Actions), not distributed.
- Попросят demo-видео флоу — сделаем вместе, я подготовлю (запись экрана: запуск скрипта → видео появляется в Studio).

## Часть 4. Пока аудита нет — ручная публикация (2–3 мин на видео)

Бот присылает тебе в Telegram готовый MP4 + название + описание + теги + обложку:
1. studio.youtube.com → Create → Upload video → перетащи файл.
2. Вставь title/description из сообщения, обложку из вложения.
3. ⚠️ Галочка **Altered content → Yes** ставится ТОЛЬКО если в сообщении бота написано «AI-disclosure: ДА» (я помечаю видео, где есть фотореалистичные AI-кадры).
4. Аудитория: «No, it's not made for kids». Visibility: Public (или Schedule на присланное время).

## Часть 5. Позже (когда придёт YPP)

- 1000 подписчиков + 4000 часов → Studio предложит подать заявку в YouTube Partner Program → AdSense-аккаунт на этот же gmail (Армения поддерживается, выплаты от $100).
- Заявку YPP подаём когда ретеншн стабильный (заходят с manual review для AI-контента, 2–4 недели).

---

**Стоимость всего выше: $0.** Когда сделаешь Части 1–2 и положишь client_secret.json — напиши мне «готово», дальше всё моё.
