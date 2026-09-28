# website-gateway-service

Микросервис для обработки сообщений от встраиваемого JS-виджета чата на сайтах клиентов.

## Описание

Пользователь вставляет JS-сниппет на свой сайт. Виджет отправляет сообщения на API с заголовком `X-Widget-Key`. Сервис валидирует ключ через control-plane, получает `agent_id`, передаёт сообщение в ai-agent-service и возвращает ответ.

Сервис stateless — нет своей базы данных. Активные интеграции и их виджет-ключи получает через polling из `control-plane-service` каждые `INTEGRATIONS_POLL_INTERVAL` секунд и держит в in-memory кэше (`WidgetKeyManager`).

## Архитектура

```
Сайт клиента (widget.js)
  │
  ├── GET  /v1/config?key=wk_abc123  (загрузка темы виджета)
  └── POST /v1/chat                  (X-Widget-Key: wk_abc123)
  │
  ▼
website-gateway-service
   │
   ├── WidgetKeyManager — кэш валидных ключей (reconcile из control-plane каждые 60с)
   │     └── GET /v1/internal/integrations?channel_type=WEBSITE_WIDGET
   │           (заголовок X-Internal-Token: $INTERNAL_API_TOKEN)
  │
  ├── ChatHandler — обработка входящих сообщений
  │     ├── Валидация X-Widget-Key
  │     ├── POST /v1/chat/completions → ai-agent-service
  │     └── Возврат ответа
  │
  ├── ConfigHandler — отдаёт настройки виджета (primary_color, position, welcome_message, window_title)
  │
  └── GET /health
```

Widget traffic path: whether виджет ходит напрямую или через chota-gateway — requires verification in the krakend.json audit (Phase 2).



## Эндпоинты

| Метод | Путь | Описание |
|-------|------|----------|
| `POST` | `/v1/chat` | Отправить сообщение от виджета |
| `GET` | `/v1/config` | Получить настройки отображения виджета |
| `GET` | `/health` | Проверка состояния сервиса |
| `GET` | `/` | Информация о сервисе (синоним `/health`, B-010) |

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

### GET /v1/config

Возвращает настройки темы/поведения виджета по его API-ключу. Используется виджетом на старте, чтобы применить брендинг до первого сообщения.

**Query parameters:**
- `key` (обязательный) — виджет-ключ (тот же, что передаётся в `X-Widget-Key` для `/v1/chat`).

**Response 200:**
```json
{
  "primary_color": "#6C5CE7",
  "position": "bottom-right",
  "welcome_message": "Привет! Чем могу помочь?",
  "window_title": "Чат с ассистентом"
}
```

Если какое-либо поле отсутствует в `credentials.widget_config` на стороне control-plane — подставляется дефолт из примера выше.

**Errors:**
- `400` — отсутствует query-параметр `key`
- `404` — ключ не найден в кэше `WidgetKeyManager`

Авторизация: ключ валидируется через `WidgetKeyManager.validate_key(key)` — отдельных заголовков не требуется.

### GET /health

**Response 200:**
```json
{
  "status": "ok",
  "active_widgets": 3
}
```

Поле `active_widgets` — размер in-memory кэша `WidgetKeyManager`. Значение `0` означает, что reconcile ещё не произошёл либо активных WEBSITE_WIDGET-интеграций в control-plane нет.

### GET /

Возвращает `{"service": "website-gateway-service", "status": "running"}`. Используется как лёгкий liveness-пинг; функционально дублирует `/health` (B-010).

## Переменные окружения

| Переменная | Обязательная | По умолчанию | Описание |
|------------|-------------|--------------|----------|
| `CONTROL_PLANE_URL` | нет | `http://control-plane-service` | URL control-plane-service |
| `INTERNAL_API_TOKEN` | нет | `""` | S2S-токен, отправляется в control-plane в заголовке `X-Internal-Token` |
| `CHAT_S2S_TOKEN` | нет | `""` | Отдельный токен для вызовов `ai-agent-service`; не переиспользуйте `INTERNAL_API_TOKEN` |
| `INTEGRATIONS_POLL_INTERVAL` | нет | `60` | Интервал reconcile с control-plane (секунды) |
| `AI_AGENT_SERVICE_URL` | нет | `http://ai-agent-service` | URL ai-agent-service |
| `AI_AGENT_REQUEST_TIMEOUT_SECONDS` | нет | `120` | Read timeout ожидания ответа от ai-agent-service при обработке widget chat request |
| `HOST` | нет | `0.0.0.0` | Хост сервера |
| `PORT` | нет | `8080` | Порт сервера |
| `ALLOWED_ORIGINS` | нет | `*` | *Currently the middleware echoes the request Origin regardless of this value (see `docs/FOUND_BUGS.md` B-001). The variable is reserved for future whitelist implementation.* |
| `LOG_LEVEL` | нет | `INFO` | Уровень логирования |

## Аутентификация / S2S

Сервис участвует в двух контурах аутентификации:

- **Входящие запросы от виджета** — заголовок `X-Widget-Key` на `/v1/chat`. Значение ключа валидируется против in-memory кэша `WidgetKeyManager`, который синхронизируется из `control-plane-service`.
- **Исходящие запросы в control-plane** — заголовок `X-Internal-Token: $INTERNAL_API_TOKEN` (см. `services/control_plane_client.py`). Это общий S2S-токен платформы — он должен быть одинаковым во всех внутренних сервисах Chota (user-service, control-plane-service, messenger gateways, website-gateway-service).

Для каждого сообщения ключ виджета проверяется через защищённый `control-plane-service` endpoint `/v1/widget/internal/validate`. Ответ содержит доверенные `agent_id` и `owner_id`; исходящий запрос к `/v1/chat/completions` отправляет владельца в `X-User-Id` и отдельный `CHAT_S2S_TOKEN` в `X-Service-Token`.

## Интеграции

**Исходящие:**

- `GET {CONTROL_PLANE_URL}/v1/internal/integrations?channel_type=WEBSITE_WIDGET`
  Заголовок: `X-Internal-Token: $INTERNAL_API_TOKEN`.
  Ожидаемая форма ответа — список объектов:
  ```json
  [
    {
      "integration_id": "uuid",
      "agent_id": "uuid",
      "owner_id": "uuid",
      "credentials": {
        "widget_api_key": "wk_abc123",
        "widget_config": {
          "primary_color": "#6C5CE7",
          "position": "bottom-right",
          "welcome_message": "...",
          "window_title": "..."
        }
      }
    }
  ]
  ```

- `POST {AI_AGENT_SERVICE_URL}/v1/chat/completions`
  Тело:
  ```json
  {
    "agent_id": "uuid",
    "external_client_id": "session_id-from-widget",
    "channel": "website_widget",
    "message": "..."
  }
  ```
  Ожидается поле `answer` в ответе — его значение отправляется обратно виджету как `reply`.

**Входящие:**
- JS-виджет с сайтов клиентов (через CORS).

## Polling / WidgetKeyManager

- При старте `main.py` вызывает `key_manager.reconcile()` один раз. Если активных WEBSITE_WIDGET-интеграций нет — логируется предупреждение, сервис всё равно стартует.
- В фоне работает `reconcile_loop()` с интервалом `INTEGRATIONS_POLL_INTERVAL` секунд.
- При ошибке обращения к control-plane кэш НЕ сбрасывается — сервис продолжает работать с предыдущим снапшотом (fail-soft).
- После успешного reconcile в кэше остаются только ключи, пришедшие из ответа; прочие удаляются.
- `validate_key(api_key)` — O(1) lookup в dict, возвращает `WidgetInfo` или `None`.

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

Всего: **18 тестов** (все — `@pytest.mark.asyncio`).

| Файл | Тестов |
|------|-------:|
| `tests/test_handlers.py` | 9 |
| `tests/test_widget_key_manager.py` | 5 |
| `tests/test_control_plane_client.py` | 2 |
| `tests/test_core_api.py` | 2 |

## Структура проекта

```
website-gateway-service/
├── main.py                         # Точка входа, запуск aiohttp + reconcile loop
├── core/
│   ├── config.py                   # Настройки из env (pydantic-settings)
│   └── logger.py                   # Логирование
├── services/
│   ├── control_plane_client.py    # HTTP клиент к control-plane (IntegrationConfig DTO)
│   ├── core_api.py                 # HTTP клиент к ai-agent-service
│   └── widget_key_manager.py       # In-memory кэш виджет-ключей (WidgetInfo DTO)
├── api/
│   ├── handlers.py                 # HTTP обработчики (chat, config, health, root)
│   └── middleware.py               # CORS middleware + preflight OPTIONS
└── tests/
    ├── conftest.py
    ├── test_handlers.py
    ├── test_widget_key_manager.py
    ├── test_control_plane_client.py
    └── test_core_api.py
```

## Troubleshooting

- **Пустой `INTERNAL_API_TOKEN`.** При пустом значении control-plane всё равно получит заголовок `X-Internal-Token:` и, скорее всего, вернёт `401`. Убедитесь, что `INTERNAL_API_TOKEN` задан одинаково в этом сервисе и в `control-plane-service`.
- **`active_widgets: 0` в `/health`.** Либо polling ещё не успел выполниться (обычно < 1 минуты от старта), либо в control-plane нет активных WEBSITE_WIDGET-интеграций, либо reconcile падает — проверьте логи.
- **Виджет получает `401 Invalid widget key`.** Кэш синхронизирован с задержкой до `INTEGRATIONS_POLL_INTERVAL` секунд. Если интеграция только что создана/обновлена — подождите следующего reconcile или рестартните сервис.
- **Виджет получает HTTP 200 с русским текстом «Извините, сервис временно недоступен. Попробуйте позже.»** При ошибке `ai-agent-service` сервис возвращает HTTP 200 со строкой-заглушкой вместо ошибки (см. `docs/FOUND_BUGS.md` B-003). Внешне запрос выглядит успешным — ищите причину в логах `core_api.py` (`Core API error: ...`) и в самом ai-agent-service.
- **`ALLOWED_ORIGINS` не применяется.** На данный момент middleware эхо-возвращает `Origin` запроса как есть, значение переменной не используется (см. B-001). Переменная зарезервирована под будущий whitelist.
