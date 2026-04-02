# website-gateway-service

Микросервис для обработки сообщений от встраиваемого JS-виджета чата на сайтах клиентов.

## Описание

Пользователь вставляет JS-сниппет на свой сайт. Виджет отправляет сообщения на API с заголовком `X-Widget-Key`. Сервис валидирует ключ через control-plane, получает `agent_id`, передаёт сообщение в ai-agent-service и возвращает ответ.

## Архитектура

```
Сайт клиента (widget.js)
  │
  POST /v1/chat  (X-Widget-Key: wk_abc123)
  │
  ▼
website-gateway-service :55590
  │
  ├── WidgetKeyManager — кэш валидных ключей (reconcile из control-plane каждые 60с)
  │     └── GET /v1/internal/integrations?channel_type=WEBSITE_WIDGET
  │
  ├── ChatHandler — обработка входящих сообщений
  │     ├── Валидация X-Widget-Key
  │     ├── POST /v1/chat/completions → ai-agent-service
  │     └── Возврат ответа
  │
  └── GET /health
```

## Эндпоинты

| Метод | Путь | Описание |
|-------|------|----------|
| `POST` | `/v1/chat` | Отправить сообщение от виджета |
| `GET` | `/health` | Проверка состояния сервиса |
| `GET` | `/` | Информация о сервисе |

### POST /v1/chat

**Headers:**
```
X-Widget-Key: wk_abc123
Content-Type: application/json
```

**Request:**
```json
{
  "session_id": "uuid-generated-by-widget",
  "message": "Привет, сколько стоит доставка?"
}
```

**Response 200:**
```json
{
  "reply": "Доставка стоит от 5 рублей...",
  "session_id": "uuid-generated-by-widget"
}
```

**Errors:**
- `401` — отсутствует или невалидный `X-Widget-Key`
- `400` — невалидный JSON или отсутствует `session_id`/`message`

## Переменные окружения

| Переменная | Обязательная | По умолчанию | Описание |
|------------|-------------|--------------|----------|
| `CONTROL_PLANE_URL` | нет | `http://localhost:55580` | URL control-plane-service |
| `INTERNAL_API_TOKEN` | нет | `""` | Токен для внутренних запросов |
| `AI_AGENT_SERVICE_URL` | нет | `http://localhost:55570` | URL ai-agent-service |
| `INTEGRATIONS_POLL_INTERVAL` | нет | `60` | Интервал синхронизации (секунды) |
| `HOST` | нет | `0.0.0.0` | Хост сервера |
| `PORT` | нет | `8080` | Порт сервера |
| `ALLOWED_ORIGINS` | нет | `*` | CORS origins |
| `LOG_LEVEL` | нет | `INFO` | Уровень логирования |

## Запуск

```bash
# Создать .env
cp .env.example .env

# Установить зависимости
pip install -r requirements.txt

# Запустить
python main.py
```

### Docker

```bash
docker compose up -d
```

## Тесты

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest tests/ -v
```

## Структура

```
website-gateway-service/
├── main.py                     # Точка входа
├── core/
│   ├── config.py               # Настройки из env
│   └── logger.py               # Логирование
├── services/
│   ├── control_plane_client.py # HTTP клиент к control-plane
│   ├── core_api.py             # HTTP клиент к ai-agent-service
│   └── widget_key_manager.py   # Кэш API-ключей виджетов
├── api/
│   ├── handlers.py             # HTTP обработчики
│   └── middleware.py           # CORS middleware
└── tests/
    ├── test_widget_key_manager.py
    ├── test_handlers.py
    ├── test_control_plane_client.py
    └── test_core_api.py
```
