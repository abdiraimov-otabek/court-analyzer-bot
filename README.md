# test_bot

MVP Telegram-бот для автоматизированного поиска и статистического анализа судебных актов из Картотеки арбитражных дел (КАД) по запросу пользователя на естественном языке. Интеграция с внешним провайдером API КАД (parser-api.com/kad-arbitr-ru) предусмотрена через адаптер.

## Ключевые требования

- Обработка запросов пользователя на естественном языке.
- Быстрая оценка объема выборки (0, 1-50, >50 дел).
- Для выборки >50 дел бот предлагает выбор квартала (1-4) и запускает анализ выбранного периода.
- Один активный запрос на пользователя.
- Лимит 10 запросов в час на пользователя.
- Кэширование результатов на 24 часа.
- Кэш по деталям дел (`CaseId`) на 24 часа для ускорения повторных и смежных запросов.
- Параллельная обработка деталей дел с адаптивной конкуррентностью (по умолчанию 6-10).
- Анонимизация Telegram ID в логах (хэш).
- Uptime 95% в рабочие часы (Пн-Пт, 9:00-18:00 МСК), внешний API КАД не учитывается.
- Целевая нагрузка до 50 уникальных пользователей в день.

## Формат ответа пользователю

1. Сводка:

```
СВОДКА ПО ЗАПРОСУ:
Суд: 9 ААС | Период: 2023 год
Всего дел: 42
Статистика: Удовлетворено - 15 (36%), Отказано - 27 (64%)
Топ-2 основания для удовлетворения: ...
Топ-2 основания для отказа: ...
```

2. Файл `.txt` с перечнем дел:

```
А40-123456/2023 | 15.03.2023 | Удовлетворено
```

Минимум 5, максимум 50 строк.

При длительном анализе (>5 минут) бот отправляет уведомление, что обработка продолжается, и завершает запрос до конца.

## Проектная структура

```
/Users/otabek-abdiraimov/Projects/test_bot
├─ src
│  ├─ core
│  ├─ domain
│  └─ services
├─ tests
├─ docs
└─ openapi
```

## Быстрый старт (локально)

```
python -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
pytest
```

## Переменные окружения

```
TELEGRAM_BOT_TOKEN=...
KAD_API_BASE_URL=https://parser-api.com/parser/arbitr_api
KAD_API_KEY=...
ADMIN_AUTH_TOKEN=...
HASH_SALT=...
DATABASE_PATH=data/app.db
LOG_LEVEL=INFO
LOG_FILE=logs/app.log
HTTPX_LOG_LEVEL=WARNING
```

Примечание: `.env` загружается автоматически при старте (используется `python-dotenv`).

## Запуск (локально)

Админ API:

```
uvicorn src.app.admin_api:app --host 0.0.0.0 --port 8000
python -m src.app.run_admin
```

Минимальная админ-панель:

```
/admin?token=ADMIN_AUTH_TOKEN
```

Telegram бот:

```
python -m src.app.run_bot
```

## Документация

- Архитектура: `/Users/otabek-abdiraimov/Projects/test_bot/docs/ARCHITECTURE.md`
- Админка: `/Users/otabek-abdiraimov/Projects/test_bot/docs/ADMIN.md`
- Тест-план: `/Users/otabek-abdiraimov/Projects/test_bot/docs/TEST_PLAN.md`
- OpenAPI: `/Users/otabek-abdiraimov/Projects/test_bot/openapi/admin_api.yaml`
- Деплой: `/Users/otabek-abdiraimov/Projects/test_bot/docs/DEPLOYMENT.md`
- Бэкапы: `/Users/otabek-abdiraimov/Projects/test_bot/docs/BACKUP_RESTORE.md`
- Обновление версий: `/Users/otabek-abdiraimov/Projects/test_bot/docs/UPDATE_VERSION.md`
- Интеграция KAD API: `/Users/otabek-abdiraimov/Projects/test_bot/docs/KAD_API.md`
- Безопасность: `/Users/otabek-abdiraimov/Projects/test_bot/docs/SECURITY.md`
