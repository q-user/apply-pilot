# Job Apply Assistant — user story и архитектурное видение

## 1. Краткое описание

`Job Apply Assistant` — многопользовательское self-hosted приложение для точечного поиска работы.

Пользователь загружает резюме, задаёт карьерные цели и подключает источники вакансий. Система периодически собирает вакансии, сохраняет их в БД, оценивает релевантность, готовит сопроводительные письма и ответы на тестовые/антибот-вопросы. Затем Telegram-бот раз в сутки или по заданному расписанию присылает статистику и проводит ревью: показывает вакансию, ключевые детали, объяснение релевантности, письмо и подготовленные ответы. Пользователь принимает, отклоняет, откладывает или просит перегенерировать письмо с комментарием. После принятия создаётся задача на отправку отклика.

Ключевой принцип: не массовая рассылка, а аккуратные точечные отклики только на наиболее релевантные вакансии.

---

## 2. Что взять из текущего `hh_apply`

1. **Docker-first запуск**: локально и на сервере через `docker compose`.
2. **Разделение процессов**: collector, Telegram bot, apply worker, API/web.
3. **Гибридный подход к hh**: API там, где хватает; Playwright там, где нужны браузерные действия, тесты, антибот-защита или сложная авторизация.
4. **AI-фильтрация вакансий**: быстрый фильтр + глубокий анализ резюме и вакансии.
5. **Генерация сопроводительных писем**: шаблоны, LLM, версии письма, перегенерация по комментарию.
6. **Сохранение решений в БД**: вакансии, причины отклонения, черновики, ответы на вопросы, статусы отправки.
7. **Telegram как основной UX принятия решений**: ежедневный дайджест, inline-кнопки, ревью одной вакансии за раз.
8. **Безопасность персональных данных**: self-hosted режим, секреты через env/Docker secrets, минимум данных во внешние LLM.

---

## 3. Выбор Python-фреймворка

### Рекомендация: FastAPI

Основной фреймворк: **FastAPI**.

Причины:

- async-first: внешний HTTP, Telegram, LLM, очереди, Playwright, PostgreSQL;
- современный backend-стек: Pydantic V2, OpenAPI, typed-first подход;
- проще и прозрачнее Django для интеграционного продукта с worker-ами;
- меньше ручной инфраструктуры, чем в aiohttp;
- хорошо подходит для vertical slice architecture;
- удобен для параллельной разработки AI-агентами в git worktrees;
- можно начать с API + Telegram, а web UI добавить позже.

### Почему не aiohttp

`aiohttp` полезен для изучения низкоуровневого async web, но как основа продукта даст больше ручной работы:

- нет такого удобного OpenAPI из коробки;
- больше boilerplate вокруг валидации, DI, ошибок и схем;
- меньше прикладной пользы для быстрого production API.

Можно использовать идеи aiohttp и async-подход, но основной фреймворк лучше FastAPI.

### Почему не Django

Django хорош, если главный продукт — CRUD/admin/SaaS с тяжёлой админкой. Здесь центр тяжести другой:

- фоновые задачи;
- Telegram workflow;
- внешние API;
- LLM pipeline;
- Playwright;
- очереди;
- асинхронные интеграции.

Django возможен, но будет тяжелее и менее естественен для такой архитектуры.

### Альтернатива для изучения

**Litestar** — перспективный async framework с хорошей архитектурной дисциплиной. Его можно изучить отдельно или рассмотреть для эксперимента, но для MVP лучше FastAPI из-за экосистемы.

Итоговый стек:

- Python 3.12+;
- FastAPI;
- Pydantic V2;
- SQLAlchemy 2.0 async;
- Alembic;
- PostgreSQL;
- Redis;
- arq или taskiq для очередей;
- aiogram для Telegram;
- httpx;
- Playwright;
- OpenAI-compatible LLM adapter;
- ruff, mypy/basedpyright, pytest;
- Docker Compose.

---

## 4. Главная user story

**Как** специалист, ищущий работу,  
**я хочу** один раз загрузить резюме, описать желаемую работу и подключить источники вакансий,  
**чтобы** приложение само находило наиболее релевантные вакансии, готовило персонализированные отклики, показывало мне их в Telegram для быстрого ревью и отправляло только после моего подтверждения.

Acceptance criteria:

- приложение запускается локально или на сервере через Docker;
- несколько пользователей могут работать в одной инсталляции;
- каждый пользователь видит только свои данные;
- hh поддерживается как первый источник;
- новые источники можно добавлять без переписывания ядра;
- вакансии сохраняются в БД до принятия решения;
- каждая вакансия имеет объяснимый score;
- сопроводительное письмо можно принять или перегенерировать;
- ответы на тестовые вопросы готовятся заранее, где возможно;
- отправка отклика происходит только после явного подтверждения пользователя;
- пользователь ежедневно получает статистику и очередь ревью в Telegram;
- система хранит историю решений, ошибок и отправок;
- проект удобно развивать параллельными AI-агентами через vertical slices и git worktrees.

---

## 5. Роли

### 5.1. Пользователь

Ищет работу и хочет получать тщательно отобранные вакансии.

Может:

- зарегистрироваться;
- подключить Telegram;
- загрузить резюме;
- задать карьерные цели;
- подключить hh;
- просматривать вакансии;
- принимать, отклонять, откладывать отклики;
- просить перегенерацию письма;
- смотреть статистику и историю.

### 5.2. Администратор

Владелец self-hosted инсталляции.

Может:

- управлять пользователями;
- смотреть состояние очередей;
- смотреть ошибки интеграций;
- настраивать лимиты;
- подключать источники;
- управлять LLM-провайдерами;
- смотреть системные метрики.

### 5.3. Worker

Системная роль фоновых процессов.

Делает:

- сбор вакансий;
- нормализацию;
- дедупликацию;
- scoring;
- генерацию писем;
- подготовку ответов на тесты;
- создание задач отправки;
- отправку откликов;
- логирование результата.

---

## 6. Epic 1. Регистрация и первичная настройка

### Story 1.1. Регистрация

**Как** новый пользователь,  
**я хочу** создать аккаунт,  
**чтобы** хранить свои резюме, настройки и историю поиска.

Acceptance criteria:

- регистрация по email и паролю;
- пароль хранится только в виде хэша;
- создаётся уникальный профиль пользователя;
- после регистрации показывается onboarding checklist;
- публичную регистрацию можно отключить в self-hosted режиме.

### Story 1.2. Подключение Telegram

**Как** пользователь,  
**я хочу** привязать Telegram,  
**чтобы** получать дайджесты и ревью вакансий в боте.

Acceptance criteria:

- пользователь получает deep link или одноразовый код;
- бот связывает Telegram chat id с пользователем;
- привязка подтверждается;
- один Telegram-аккаунт не привязывается к двум пользователям случайно;
- Telegram можно отвязать.

### Story 1.3. Загрузка резюме

**Как** пользователь,  
**я хочу** загрузить резюме,  
**чтобы** система могла оценивать вакансии и писать письма.

Acceptance criteria:

- поддерживаются PDF, DOCX, TXT, Markdown;
- текст извлекается и сохраняется отдельно;
- пользователь может отредактировать распознанный текст;
- у пользователя может быть несколько резюме;
- одно резюме можно сделать активным.

### Story 1.4. Настройка search profile

**Как** пользователь,  
**я хочу** задать параметры желаемой работы,  
**чтобы** система искала только подходящие вакансии.

Параметры:

- желаемые должности;
- грейд;
- стек;
- зарплата;
- валюта;
- формат работы;
- локации;
- часовой пояс;
- тип занятости;
- языки;
- стоп-компании;
- стоп-слова;
- минимальный score;
- лимит вакансий в дайджесте;
- лимит отправок в сутки.

Acceptance criteria:

- пользователь может создать несколько search profiles;
- каждый profile связан с резюме;
- profile можно включить/выключить;
- настройки используются collector-ом.

---

## 7. Epic 2. Интеграция с hh

### Story 2.1. Подключение hh-аккаунта

**Как** пользователь,  
**я хочу** подключить hh-аккаунт,  
**чтобы** приложение могло искать вакансии и отправлять отклики.

Acceptance criteria:

- токены хранятся безопасно;
- пользователь видит статус подключения;
- приложение умеет обновлять токен;
- при истечении авторизации пользователь получает уведомление;
- Playwright используется только там, где API недостаточно.

### Story 2.2. Синхронизация резюме с hh

**Как** пользователь,  
**я хочу** связать локальное резюме с резюме на hh,  
**чтобы** отклики отправлялись с правильным resume id.

Acceptance criteria:

- приложение получает список резюме на hh;
- пользователь выбирает соответствие;
- mapping сохраняется;
- если резюме недоступно, пользователь получает предупреждение.

### Story 2.3. Сбор вакансий с hh

**Как** пользователь,  
**я хочу** чтобы приложение регулярно искало вакансии на hh,  
**чтобы** не мониторить сайт вручную.

Acceptance criteria:

- collector запускается по расписанию;
- для каждого активного search profile создаётся задача поиска;
- вакансии сохраняются в БД;
- дубликаты склеиваются по source, source_id, canonical_url;
- raw payload сохраняется;
- API ошибки логируются;
- используется rate limit.

### Story 2.4. Тестовые/антибот-вопросы

**Как** пользователь,  
**я хочу** чтобы приложение заранее готовило ответы на тестовые вопросы,  
**чтобы** не застревать при отправке отклика.

Acceptance criteria:

- система извлекает вопросы, если это технически доступно;
- вопросы классифицируются: text, single choice, multiple choice, captcha/anti-bot;
- для текстовых вопросов генерируется ответ;
- для choice-вопросов выбирается подходящий вариант;
- пользователь видит подготовленные ответы при ревью;
- при низкой уверенности вакансия получает статус `needs_manual_review`.

---

## 8. Epic 3. Другие источники вакансий

### Story 3.1. Единый интерфейс источника

**Как** разработчик,  
**я хочу** иметь общий интерфейс source adapter,  
**чтобы** добавлять hh, Telegram-каналы, сайты компаний и другие job boards независимо.

Acceptance criteria:

- каждый источник реализует общий port/interface;
- adapter возвращает нормализованный `VacancyDraft`;
- raw данные сохраняются отдельно;
- источник не знает о Telegram-боте и отправке;
- новый источник разрабатывается отдельным vertical slice.

### Story 3.2. Telegram-каналы

**Как** пользователь,  
**я хочу** подключить Telegram-каналы с вакансиями,  
**чтобы** находить предложения вне hh.

Acceptance criteria:

- пользователь добавляет список каналов;
- система читает новые сообщения через допустимый adapter;
- сообщения классифицируются как вакансия/не вакансия;
- извлекаются title, company, stack, salary, location, contacts, apply link;
- вакансии проходят общий scoring pipeline.

### Story 3.3. Сайты компаний

**Как** пользователь,  
**я хочу** добавить карьерные страницы компаний,  
**чтобы** получать вакансии напрямую.

Acceptance criteria:

- пользователь добавляет URL;
- система периодически проверяет изменения;
- вакансии нормализуются;
- дубликаты с другими источниками склеиваются;
- для JS-heavy сайтов используется Playwright fetcher.

---

## 9. Epic 4. Scoring и отбор

### Story 4.1. Quick filter

**Как** система,  
**я хочу** быстро отсеивать очевидно неподходящие вакансии,  
**чтобы** не тратить LLM-бюджет.

Acceptance criteria:

- учитываются title, salary, location, remote flag, keywords, stop words;
- сохраняется `quick_score`;
- при отклонении сохраняется причина;
- пользователь может посмотреть причины пропуска.

### Story 4.2. Deep AI scoring

**Как** пользователь,  
**я хочу** чтобы система тщательно сравнивала вакансию с резюме,  
**чтобы** в ревью попадали только сильные совпадения.

Acceptance criteria:

- deep scoring запускается после quick filter;
- модель получает структурированное резюме, вакансию и search profile;
- результат содержит score 0-100, summary, сильные совпадения, риски, missing requirements, recommended action;
- результат сохраняется;
- решение объяснимо пользователю.

### Story 4.3. Strict mode

**Как** пользователь,  
**я хочу** включить строгий режим отбора,  
**чтобы** получать меньше, но точнее.

Acceptance criteria:

- strict mode повышает минимальный score;
- сильнее штрафуются mismatch по грейду, зарплате, формату и стеку;
- strict mode включается на уровне search profile.

---

## 10. Epic 5. Сопроводительные письма

### Story 5.1. Первый черновик

**Как** пользователь,  
**я хочу** получать готовое письмо,  
**чтобы** не писать его вручную.

Acceptance criteria:

- письмо генерируется только для вакансий после scoring;
- учитываются резюме, вакансия, компания и preferences;
- письмо не выдумывает опыт;
- письмо сохраняется как versioned draft;
- пользователь видит письмо в Telegram/web.

### Story 5.2. Перегенерация

**Как** пользователь,  
**я хочу** написать комментарий к письму,  
**чтобы** система изменила его стиль или акценты.

Acceptance criteria:

- кнопка `Перегенерировать`;
- бот просит комментарий;
- комментарий сохраняется;
- новая версия создаётся отдельно;
- предыдущие версии не теряются;
- пользователь может принять любую версию.

### Story 5.3. Стиль письма

**Как** пользователь,  
**я хочу** выбрать стиль письма,  
**чтобы** отклик соответствовал моей манере общения.

Варианты:

- кратко и по делу;
- дружелюбно;
- формально;
- senior/expert;
- стартапный стиль;
- кастомный prompt.

Acceptance criteria:

- стиль задаётся в search profile;
- стиль можно переопределить для конкретной вакансии;
- стиль влияет на форму, но не на факты.

---

## 11. Epic 6. Telegram workflow

### Story 6.1. Ежедневная статистика

**Как** пользователь,  
**я хочу** получать ежедневную статистику,  
**чтобы** понимать, что система сделала.

Сообщение включает:

- найдено вакансий;
- новых;
- отклонено quick filter;
- отклонено deep scoring;
- подготовлено к ревью;
- ожидает отправки;
- отправлено;
- ошибок;
- топ причин отклонения.

Acceptance criteria:

- время дайджеста настраивается;
- если новых вакансий нет, бот сообщает кратко;
- ошибки интеграции показываются понятным текстом;
- статистика строится по каждому search profile.

### Story 6.2. Ревью вакансии

**Как** пользователь,  
**я хочу** получать вакансии одну за одной,  
**чтобы** быстро принимать решение.

Карточка содержит:

- title;
- company;
- salary;
- location/remote;
- grade;
- stack;
- source;
- ссылку;
- краткое описание;
- почему подходит;
- риски;
- score;
- письмо;
- подготовленные ответы.

Кнопки:

- `Принять`;
- `Отклонить`;
- `Перегенерировать письмо`;
- `Показать подробнее`;
- `Отложить`;
- `Заблокировать компанию`;
- `Изменить настройки фильтра`.

Acceptance criteria:

- после решения показывается следующая вакансия;
- ревью можно остановить и продолжить позже;
- все действия пишутся в audit log;
- повторное нажатие не создаёт дубль задачи.

### Story 6.3. Принятие отклика

**Как** пользователь,  
**я хочу** принять отклик,  
**чтобы** система отправила его автоматически.

Acceptance criteria:

- после `Принять` создаётся `apply_job`;
- статус job: `queued`;
- дубль не создаётся;
- пользователь видит подтверждение;
- worker отправляет отклик по лимитам.

### Story 6.4. Отклонение

**Как** пользователь,  
**я хочу** отклонить вакансию с причиной,  
**чтобы** система лучше понимала мои предпочтения.

Acceptance criteria:

- можно выбрать типовую причину;
- можно написать свою;
- причина сохраняется;
- вакансия не возвращается в ревью без явного сброса.

---

## 12. Epic 7. Очередь отправки

### Story 7.1. Apply worker

**Как** система,  
**я хочу** отправлять только одобренные отклики,  
**чтобы** пользователь сохранял контроль.

Acceptance criteria:

- worker берёт только `apply_job` со статусом `queued`;
- проверяет активность пользователя и профиля;
- соблюдает per-user и per-source rate limit;
- делает retry с backoff;
- сохраняет результат;
- уведомляет пользователя, если нужна ручная помощь.

### Story 7.2. Отправка на hh

**Как** пользователь,  
**я хочу** чтобы система отправила отклик на hh с выбранным резюме и письмом.

Acceptance criteria:

- используется правильный hh resume id;
- используется принятая версия письма;
- используются утверждённые ответы на вопросы;
- при дополнительном требовании job получает `blocked_manual_action`;
- повторная отправка на одну вакансию невозможна без override.

### Story 7.3. Антиспам и лимиты

**Как** администратор,  
**я хочу** настроить лимиты,  
**чтобы** снизить риск блокировок и сохранить качество.

Acceptance criteria:

- лимиты глобальные, пользовательские и source-specific;
- есть случайная задержка между отправками;
- есть quiet hours;
- пользователь видит, почему job ждёт отправки.

---

## 13. Epic 8. Web/API кабинет

### Story 8.1. Dashboard

**Как** пользователь,  
**я хочу** видеть dashboard,  
**чтобы** понимать состояние поиска.

Показывает:

- активные search profiles;
- статус источников;
- найденные вакансии;
- отправленные отклики;
- pending review;
- queued jobs;
- последние ошибки;
- причины отклонения.

Acceptance criteria:

- доступ только авторизованному пользователю;
- пользователь видит только свои данные;
- API документирован через OpenAPI.

### Story 8.2. Управление вакансиями

**Как** пользователь,  
**я хочу** просматривать найденные вакансии в web UI,  
**чтобы** анализировать историю и вручную принимать решения.

Acceptance criteria:

- фильтры по статусу, source, score, company, date;
- карточка вакансии;
- accept/reject/defer;
- экспорт CSV/JSON;
- raw payload доступен только в admin/debug режиме.

### Story 8.3. Админка

**Как** администратор,  
**я хочу** управлять состоянием системы.

Acceptance criteria:

- список пользователей;
- состояние очередей;
- последние ошибки;
- restart failed job;
- отключение источника;
- версия приложения и миграций.

---

## 14. Архитектура runtime

Минимальный `docker-compose`:

1. `api` — FastAPI, REST, auth, health checks.
2. `telegram_bot` — aiogram, long polling.
3. `scheduler` — периодический запуск collector/scoring.
4. `worker` — очереди, scoring, generation, apply jobs.
5. `postgres` — основная БД.
6. `redis` — broker, locks, rate limit, transient state.
7. `playwright_worker` — опционально отдельный worker для браузерных задач.

PostgreSQL выбран вместо SQLite, потому что приложение многопользовательское: concurrent writes, job statuses, audit log, аналитика, полнотекстовый поиск, миграции.

SQLite можно оставить только для dev/demo режима.

---

## 15. Вертикальная архитектура

Цель: разные AI-агенты могут независимо брать feature slice, работать в отдельных git worktrees и минимально конфликтовать.

Принципы:

1. Feature-first структура.
2. Shared только для стабильных примитивов.
3. Каждая вертикаль содержит свои API, models, schemas, service, repository, tests.
4. Границы через ports/interfaces.
5. Минимум глобальных связей.
6. Shared-код добавляется после второго использования.

Пример структуры:

```text
src/job_apply/
  main.py
  config.py
  container.py

  shared/
    auth/
    db/
    errors/
    events/
    logging/
    pagination/
    security/
    telemetry/
    utils/

  features/
    users/
      api.py
      models.py
      schemas.py
      service.py
      repository.py
      tests/

    resumes/
      api.py
      models.py
      schemas.py
      parser.py
      service.py
      repository.py
      tests/

    search_profiles/
      api.py
      models.py
      schemas.py
      service.py
      repository.py
      tests/

    vacancy_sources/
      ports.py
      registry.py
      api.py
      tests/

    hh_source/
      adapter.py
      auth.py
      client.py
      models.py
      service.py
      tests/

    telegram_source/
      adapter.py
      parser.py
      service.py
      tests/

    vacancies/
      api.py
      models.py
      schemas.py
      normalization.py
      repository.py
      tests/

    scoring/
      api.py
      models.py
      prompts.py
      service.py
      tests/

    cover_letters/
      api.py
      models.py
      prompts.py
      service.py
      tests/

    screening_questions/
      api.py
      models.py
      prompts.py
      service.py
      tests/

    review_queue/
      api.py
      models.py
      service.py
      telegram_handlers.py
      tests/

    apply_jobs/
      api.py
      models.py
      worker.py
      service.py
      tests/

    telegram_bot/
      bot.py
      router.py
      keyboards.py
      handlers.py
      tests/

  workers/
    scheduler.py
    worker.py
    playwright_worker.py

  migrations/
```

Правила для AI-агентов:

- один агент — одна feature-директория;
- общие контракты меняются отдельной задачей;
- миграции именуются по feature и issue id;
- тесты colocated рядом с feature;
- не менять чужую feature без явной причины;
- большие изменения делать в отдельном git worktree;
- каждый PR содержит feature change, миграцию, тесты, обновление документации при необходимости.

---

## 16. Предварительная модель данных

### User

- id;
- email;
- password_hash;
- role;
- status;
- created_at;
- updated_at.

### TelegramAccount

- id;
- user_id;
- telegram_user_id;
- chat_id;
- username;
- linked_at;
- is_active.

### Resume

- id;
- user_id;
- title;
- original_file_path/object_key;
- original_file_type;
- extracted_text;
- structured_profile_json;
- is_active;
- created_at;
- updated_at.

### SearchProfile

- id;
- user_id;
- resume_id;
- title;
- desired_titles;
- skills;
- salary_min;
- currency;
- locations;
- remote_mode;
- employment_types;
- stop_words;
- stop_companies;
- min_score;
- max_daily_reviews;
- max_daily_applies;
- digest_time;
- strict_mode;
- is_active.

### Vacancy

- id;
- source_type;
- source_id;
- canonical_url;
- title;
- company_name;
- salary_from;
- salary_to;
- currency;
- location;
- remote_mode;
- description;
- skills;
- raw_payload_json;
- first_seen_at;
- last_seen_at.

### VacancyMatch

Связь вакансии с конкретным пользователем и search profile.

- id;
- user_id;
- search_profile_id;
- resume_id;
- vacancy_id;
- quick_score;
- deep_score;
- status;
- decision_reason;
- match_summary;
- risks_json;
- missing_requirements_json;
- created_at;
- updated_at.

Статусы:

- `new`;
- `quick_rejected`;
- `deep_rejected`;
- `ready_for_review`;
- `reviewing`;
- `approved`;
- `rejected_by_user`;
- `deferred`;
- `queued_for_apply`;
- `applied`;
- `failed`.

### CoverLetterDraft

- id;
- vacancy_match_id;
- version;
- text;
- style;
- generation_prompt_hash;
- user_comment;
- status;
- created_at.

### ScreeningQuestionAnswer

- id;
- vacancy_match_id;
- question_text;
- question_type;
- answer_text;
- selected_options_json;
- confidence;
- explanation;
- status;
- created_at.

### ApplyJob

- id;
- user_id;
- vacancy_match_id;
- cover_letter_draft_id;
- status;
- priority;
- scheduled_at;
- attempts;
- last_error;
- applied_at;
- created_at;
- updated_at.

Статусы:

- `queued`;
- `running`;
- `retrying`;
- `success`;
- `failed`;
- `blocked_manual_action`;
- `cancelled`.

### AuditLog

- id;
- user_id;
- actor_type;
- action;
- entity_type;
- entity_id;
- metadata_json;
- created_at.

---

## 17. Основные pipeline-ы

### 17.1. Сбор вакансий

```text
scheduler
  -> create collect task per active search profile
  -> source adapter fetches vacancies
  -> normalize vacancy
  -> upsert vacancy
  -> create/update vacancy_match
  -> quick filter
  -> deep scoring for promising matches
  -> generate cover letter
  -> prepare screening answers
  -> mark ready_for_review
```

### 17.2. Telegram review

```text
daily digest
  -> user clicks Review
  -> bot sends vacancy card
  -> user accepts / rejects / regenerates / defers
  -> accepted match creates apply_job
  -> bot sends next vacancy
```

### 17.3. Отправка отклика

```text
worker
  -> fetch queued apply_job
  -> acquire per-user/source lock
  -> check limits
  -> load credentials
  -> send response through source adapter
  -> update status
  -> notify user if needed
```

---

## 18. Безопасность

1. Пароли через Argon2 или bcrypt.
2. Токены источников шифруются application key-ем.
3. Секреты через `.env` или Docker secrets.
4. Пользователь видит, какие данные уходят в LLM.
5. Можно включить redaction персональных данных перед LLM.
6. Все действия worker-ов пишутся в audit log.
7. Multi-tenant isolation через `user_id` во всех запросах.
8. Нельзя получить чужие данные через API.
9. Rate limit на API и Telegram actions.
10. Логи не содержат access token, refresh token, пароль, полный текст резюме без debug-флага.

---

## 19. Observability

MVP:

- structured JSON logs;
- request id;
- job id;
- безопасный user id;
- health endpoints;
- worker heartbeat;
- таблица failed jobs;
- Telegram-уведомление администратору при критичных ошибках.

Позже:

- OpenTelemetry;
- Prometheus;
- Grafana;
- Sentry/self-hosted аналог.

---

## 20. MVP scope

Входит:

1. Docker Compose.
2. FastAPI API.
3. PostgreSQL.
4. Redis.
5. Alembic migrations.
6. Регистрация/логин.
7. Telegram linking.
8. Загрузка одного резюме.
9. Один search profile.
10. hh как первый источник.
11. Сбор вакансий по расписанию.
12. Quick filter.
13. Deep scoring через LLM.
14. Генерация письма.
15. Подготовка простых ответов на тестовые вопросы, если доступно.
16. Ежедневный Telegram digest.
17. Telegram review одной вакансии за раз.
18. Accept/reject/regenerate/defer.
19. Очередь apply jobs.
20. Worker отправки hh-откликов.
21. История статусов.
22. Базовый admin health.

Не входит:

- полноценный frontend dashboard;
- платежи;
- CRM;
- много источников сразу;
- обучение модели на действиях пользователя;
- mobile app;
- сложная аналитика.

---

## 21. Roadmap

### Phase 0. Foundation

- репозиторий;
- `uv`;
- FastAPI app factory;
- PostgreSQL + Alembic;
- Redis;
- Dockerfile;
- docker-compose;
- ruff, mypy, pytest;
- vertical slice skeleton;
- CI.

### Phase 1. Users, resumes, Telegram

- auth;
- users;
- Telegram linking;
- resume upload;
- text extraction;
- search profile CRUD.

### Phase 2. hh collector

- hh adapter;
- credentials storage;
- vacancy fetch;
- normalization;
- deduplication;
- vacancy_match creation.

### Phase 3. Scoring and drafts

- quick filter;
- deep LLM scoring;
- cover letter generation;
- screening question preparation;
- explanation storage.

### Phase 4. Telegram review loop

- daily digest;
- vacancy card;
- accept/reject/defer;
- regenerate letter;
- create apply job.

### Phase 5. Apply worker

- queue worker;
- hh apply adapter;
- rate limit;
- retries;
- status history;
- notifications.

### Phase 6. Web dashboard

- dashboard API;
- minimal frontend;
- vacancy list;
- profile settings;
- admin panel.

### Phase 7. Additional sources

- Telegram channels;
- company career pages;
- Habr Career;
- other job boards.

### Phase 8. Intelligence improvements

- learning from user rejections;
- prompt versioning;
- A/B testing scoring prompts;
- personal style memory;
- analytics by source/profile.

---

## 22. Definition of Done для feature

Feature готова, если:

1. Есть API/use case или worker handler.
2. Есть unit tests.
3. Есть integration test для критичного пути.
4. Есть миграция, если меняется БД.
5. Есть typed schemas.
6. Ошибки домена явно описаны.
7. Логи содержат action/job id.
8. Нет утечки секретов в логах.
9. Docker запуск не сломан.
10. Feature можно разрабатывать изолированно в worktree.

---

## 23. Нефункциональные требования

### Производительность

- сотни вакансий на пользователя в сутки;
- deep scoring только после quick filter;
- Telegram bot отвечает на простые действия за 1-2 секунды;
- долгие операции уходят в очередь.

### Надёжность

- падение одного source adapter не ломает остальные;
- failed jobs можно повторить;
- все внешние вызовы имеют timeout;
- retry только для retryable ошибок;
- idempotency key для отправки отклика.

### Масштабирование

- API stateless;
- workers масштабируются горизонтально;
- Redis locks предотвращают двойную отправку;
- PostgreSQL — source of truth.

### Поддерживаемость

- vertical slices;
- typed code;
- явные контракты;
- prompt versioning;
- Alembic migrations;
- минимум глобальной магии.

---

## 24. Итоговое техническое решение

**FastAPI-монолит с вертикальными slices и отдельными runtime-процессами для API, Telegram bot, scheduler и workers.**

Это проще микросервисов, но достаточно модульно для:

- multi-user self-hosted продукта;
- Docker-запуска локально и на VPS;
- hh как первого источника;
- будущих источников вакансий;
- AI scoring/generation pipeline;
- Telegram-first UX;
- безопасной параллельной разработки AI-агентами через git worktrees.
