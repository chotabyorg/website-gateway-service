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

## Goal4A managed Website routing

The default configuration remains entirely legacy. Routing uses the authenticated
Control Plane `integration_id`; widget keys, public owner/agent fields and message
text never select a cohort. `MANAGED_WEBSITE_GUARDED_INTEGRATIONS` is a JSON mapping
of canonical integration UUID to expected agent UUID (maximum 64). Once a managed
message can be admitted, retain its guard and Conversation configuration through
rollback. Removing a guard, reverting to a gateway image without this protocol or
downgrading the Conversation database is not a supported rollback.

`MANAGED_WEBSITE_COHORTS` maps integration UUID to
`{"expected_agent_id":"<UUID>","mode":"SESSION_ALLOWLIST","session_ids":["<UUID>"]}`.
Maximum 64 integrations and 64 distinct sessions per integration. Cohorts must have
a matching retained guard. Unknown fields/modes, duplicate JSON keys, wildcard
identities and malformed/noncanonical UUIDs fail startup. Source also supports
`INTEGRATION_ALL` for one exact configured integration with `session_ids: []`;
Goal4A production/release gates prohibit deploying that mode. Production activation
is restricted to one explicitly reviewed technical session. Full demo activation
requires stable frontend IDs and a separate reviewed assistant-history/context-limit
policy; managed history in Goal4A contains inbound messages only.

For a selected managed request the body is:

```json
{"session_id":"<canonical UUID>","message_id":"<canonical UUID>","message":"exact customer text"}
```

The client generates `message_id` once per message and retains the whole envelope
on retry. The same text with a new ID is a new message; changed text/session under
the same ID conflicts (`409`). Legacy sessions without `message_id` keep the old
contract. Identified requests on guarded integrations always obtain an atomic
Conversation disposition, including when the cohort has been disabled. A retained
MANAGED disposition wins rollback; a retained LEGACY disposition wins activation
races. Only a positively committed LEGACY disposition permits legacy AI. Conversation
errors, admission ambiguity, pending/failed/unknown execution return bounded `503`
errors and never fall through to legacy.

Required when any guard exists: `CONVERSATION_SERVICE_URL` and dedicated
`CONVERSATION_S2S_TOKEN` (never reuse widget/internal/chat credentials). Private paths
are `/internal/v1/website/admissions`, `/results`, `/responses/begin`, and
`/responses/complete`. Owner/integration/agent come only from Control Plane. The
server namespace is `main-chota.website-widget.v1`, channel `WEBSITE_WIDGET`.
The worker processes each admitted message immediately in durable session order,
one logical turn per message; HTTP disconnect does not cancel worker execution.

`CONVERSATION_REQUEST_TIMEOUT_SECONDS` is 0.1–30 (default 10),
`CONVERSATION_RESULT_WAIT_SECONDS` is 0–120 (default 120), and
`CONVERSATION_RESULT_POLL_SECONDS` is 0.05–5 (default 0.25). A retained draft keeps
`{"reply":"...","session_id":"..."}`. Before writing, the gateway commits a
response intent and sets CORS and `Cache-Control: no-store`. It retains the
validated Conversation `admission_id` and derives the response-attempt UUID as
`uuid5(NAMESPACE_URL, 'main-chota.website-widget-response.v2:' + admission_id)`,
so equal public `message_id` values in different owner or integration scopes stay
independent. Conversation returns an explicit `SEND`, `REPLAY_COMPLETED`, or
`DENY` action. `SEND` permits one socket write and completion. `REPLAY_COMPLETED`
returns the same public JSON from successful same-admission evidence without
creating another attempt or completion. `DENY` omits `reply`: a superseded Turn
returns HTTP 409, while unresolved or failed transport evidence returns HTTP 503.
`WRITE_COMPLETED` means local socket completion, not browser acknowledgement;
failed, cancelled, and uncertain writes remain honest transport facts. Unknown,
failed, and superseded attempts never authorize a resend, and a no-send attempt
cannot append an AGENT Message. Reconciliation is bounded and joined even on
cancellation.

## Common credential session transport (default off)

`POST /v1/sessions` accepts an empty JSON object and issues a fresh session/token.
`POST /v1/sessions/messages` accepts only `session_id`, `message_id`, `message` and
returns a bounded `202` admission receipt using the caller's message UUID.
`POST /v1/sessions/history` accepts only `session_id` and optional opaque `cursor`;
the response is Conversation's visitor projection, bounded to 50 items/262144
serialized UTF-8 bytes. All responses use `Cache-Control: no-store`. Access uses
`X-Widget-Key` and `X-Widget-Session-Token`; raw tokens are forwarded only in the
verified private JSON body, never in query URLs or private request headers.

`COMMON_WEBSITE_INTEGRATIONS` is an explicit integration→agent JSON map, empty by
default, and must match the existing pinned guards. Every operation revalidates
the Widget key with Control Plane and derives owner/agent/current binding from
that private response. The private client requires
`COMMON_WEBSITE_CONVERSATION_URL` (HTTPS), exact
`COMMON_WEBSITE_CONVERSATION_PEER`, optional trusted CA file and dedicated
`COMMON_WEBSITE_S2S_TOKEN`. It never retries a private operation automatically.

`COMMON_WEBSITE_MESSAGE_SESSION_ALLOWLIST` is a separate new-message admission
fence, with explicit empty default `{}`. It maps selected common integration UUIDs
to lists of canonical server-issued session UUIDs, for example
`{"<integration UUID>":["<server-issued session UUID>"]}`. Until a reviewed session
is selected, even a valid session capability cannot admit CUSTOMER input through
`POST /v1/sessions/messages`: the gateway returns bounded `404 Session unavailable`
with `Cache-Control: no-store`, without calling private admission or legacy AI.
The limit is 200000 JSON characters, 64 integrations and 64 distinct sessions per
integration. Duplicate keys/sessions, wildcard or noncanonical UUIDs, unknown
integration keys, non-list values and broad modes such as `INTEGRATION_ALL` fail
startup. An empty list selects no sessions. This setting does not use the legacy
`MANAGED_WEBSITE_COHORTS` policy.

Selection uses the exact integration from fresh Control Plane validation and the
request's server session UUID after token-format validation. It never replaces
the private capability, binding, current common grant or AI lifecycle checks.
Create a genuine fresh session through `POST /v1/sessions` under the active grant,
then select its UUID through reviewed runtime configuration; never select a
client-generated visitor ID. The configuration is read at startup, so changes
require the normal reviewed process rollout. Creation, authenticated own history
and idle poll retain their existing gates and do not require message selection.
Removing a session blocks subsequent input on processes using the new config;
it does not cancel an admitted Turn, revoke authority or terminate its writer.
Use the existing CP lifecycle for authority revocation, and retain all legacy
guards/drains through rollback. Source publication alone enrolls no session.

Public HTTPS is required before reading a body or sending credentials to Control
Plane. Direct TLS uses the actual TCP peer IP. With explicitly configured
`COMMON_WEBSITE_TRUSTED_PROXY_CIDRS` (up to 16 exact networks, no `/0`), only that
TCP peer may assert a single `X-Forwarded-Proto: https` and single valid `X-Real-IP`.
Untrusted forwarding, duplicate headers and arbitrary `X-Forwarded-For` do not
provide trust. Creation forwards this derived client IP to Conversation's durable
rate admission; raw IP is not a session identity. Closed bodies are limited to
65536 UTF-8 JSON bytes, including escaping; errors expose no private payload.

Do not activate this cohort yet. Idle polling/socket ownership, positive legacy
writer retirement, trusted production writer proof, deployed TLS/privacy checks,
owner Inbox and Widget integration remain acceptance gates. This source change
does not modify production cohorts, provision visitors or establish hosted E2E.

## Credential-safe diagnostics

Chat validation requires the private Control Plane `conversation_required` boolean
and positive integer `binding_generation`. Missing or malformed fields return a
bounded 503. Deploy the compatible Control Plane reader first. A retained
Conversation enrollment makes the legacy `/v1/chat` return `409 session_required`,
including when gateway cohort configuration was removed. Fresh validation runs
on every request; browser fields and the reconciled widget appearance cache cannot
clear the routing fence. The credential-bearing session transport is a separate
rollout gate and is not enabled by this change. Initial enrollment still requires
the pinned gateway guard and positive retirement of legacy writers.

Startup installs record and output-sink protections before clients start. HTTPX,
httpcore and aiohttp dependency/access/error records are suppressed at every level,
including descendants; the actual AppRunner uses `access_log=None`. Supported
`setup_logging()` reconfiguration retains protection at INFO/DEBUG. Bounded application
events contain only fixed event codes, status, duration, safe exception type and
validated canonical UUID diagnostics. Arbitrary legacy session values keep their
existing parser semantics but appear only as `opaque` in logs. Raw exception values,
tracebacks/chains, response bodies, query URLs, headers and config objects are never
application diagnostics. Unexpected request errors return a fixed safe error.

A deployed logging proof for Website, Control Plane and any credential-observing
proxy is required before using a real widget credential. The tests use only fake
sentinels and exercise success/404/status/transport/config failures, real AppRunner
requests, opaque sessions, dependency descendants and logging reconfiguration.

## Source publication and production promotion

A push to source `main` calls the shared build/publish workflow and updates only
GitOps `releases/website-gateway-service`. Before merging Website source, create
that release branch from current valid GitOps `main` if absent; if it already
exists, inspect and advance it without discarding other work. It must contain
`services/website-gateway-service/prod/kustomization.yaml`. The shared workflow
updates `newTag` and also replaces an existing image `digest`, so pinning a digest
alone cannot contain an automatic publication to GitOps `main`.

After CI completes, record the full source SHA, actual published GHCR digest and
OCI provenance, verify the Main-node platform, and test that immutable artifact.
Source review and local Docker tests establish source behavior; they do not approve
the later CI image or establish production deployment.

Promote only the reviewed Website image reference and required reviewed config,
patches and sealed-secret changes through a separate exact-candidate GitOps PR to
`main`, after the Goal4A independent release review returns CLEAN. Do not merge the
whole release branch blindly. ArgoCD reconciles GitOps `main`; publication on the
isolated release branch does not change production desired state. Record GitOps
revision, ArgoCD revision and actual ready runtime images separately. The concrete
cross-service staging, credential-proof, canary and rollback gates remain in
GitOps `services/conversation-service/RELEASE.md`.


## Production writer attestor and exact legacy drain (default off)

The container now starts `python -m services.writer_attestor` as PID1. This small
parent starts one `services.writer_child`, mints fresh supplier/process IDs and
a private Unix-socket capability, and retains the actual OS child handle. The
child cannot fork or launch sender subprocesses; existing text transports run
inline. The same `writer_attestor.py`, `writer_child.py`, and `legacy_drain.py`
are mirrored in Telegram and Website, with identical native process tests.

Configure the parent using environment variables (not child `.env` settings):

- `WRITER_ATTESTOR_SCOPES`: JSON list of exact
  `{owner_id,integration_id,agent_id,channel}` entries (maximum 64), empty by default.
  Channels are `TELEGRAM` or `WEBSITE_WIDGET`. A configured integration whose
  scope/context does not match fails closed.
- `WRITER_PRODUCER_URL` and `WRITER_PRODUCER_TOKEN`: dedicated Conversation producer
  URL/token. The token has only matching registration/witness scope, never send,
  proof-reader or retirement authority. Parent removes producer, proof-reader
  and retirement credentials from the child's environment. Same-UID arbitrary
  code execution is outside this trusted-process capability boundary.
- `WRITER_LEGACY_DRAIN_SCOPES`: separate retained JSON list containing the same
  exact scope plus positive integer `lifecycle_generation` and `drain_generation`.
  Every attested scope requires a matching drain entry at parent startup. Retain
  this list when rolling back active channel cohorts; deleting it is not a safe
  rollback. It fences raw/v1 new work and unsupported replies. Telegram pauses
  before advancing provider offsets; Website returns 503 before body/admission.

An attested writer registers its exact text-free one-shot descriptor before Begin.
Registration is not acquisition. Only existing Conversation Begin may authorize
a send; ambiguous Begin never sends. The exact original begin/provider/socket
tasks are joined through repeated cancellation, the local slot is permanently
closed, and the parent durably uploads `TASKS_FENCED_AND_JOINED` before original
completion. Website registration excludes visitor tokens and Conversation derives
the canonical root from the retained session scope. Lost network ACKs retain and
retry identical registration/witness IDs and timestamps. A closed local slot never
reopens. Witness failure holds ownership and leaves delivery UNKNOWN.

Only actual `wait()` of the exact child allows `PROCESS_TERMINATED`; signals,
timeouts and missing PIDs are not proof. After exit the parent persists closure
for every registered slot before starting a replacement. Whole-parent loss before
durable witness leaves UNKNOWN/LIVE; Conversation retains already acknowledged
witnesses and its recovery worker owns retirement. No gateway retirement credential
is necessary. Parent metadata has a 4096-slot fail-closed bound per child lifetime;
capacity exhaustion requires a controlled child restart, not descriptor eviction.

The child publishes a private `drain.json` next to its Unix socket under
`/tmp/chota-attestor-*` (the exact path is `WRITER_CHILD_DRAIN_RECEIPT`). Receipts
include configured scope/generations, fresh supplier/process identity and acquired,
active and positively joined task counts. They explicitly cover the current
process only. They cannot attest a preceding unsupervised process or erase an
existing uncertain legacy delivery. Before enrollment, separately prove old
runtime drain, inspect retained Conversation attempts/Turns/grants and preserve
the exact admission fence. Do not enable broader cohorts as part of this change.

Native tests use real processes and pipes, actual child kill/wait, parent SIGKILL
before/after durable evidence, lost registration/witness ACKs, wrong IDs, repeated
cancellation and positive joins. Linux PR CI also builds the image without
publishing it. There are no local Docker, production, provider or model calls
in these tests. Rollout still requires exact-source/image/security/authority review.

Attestor retry bounds: each immutable slot retains at most one registration task
and one closure task. Local retries observe those same operations; at most 64
IPC request/body waiters are admitted, and a pending response returns 503 after
100 ms while its durable work continues. The child retries that pending status
without creating a new descriptor or witness. A recognized authoritative initial
registration denial (authentication/scope, missing intent, or losing-contender
transition conflict) is retained as rejected and does not block child replacement;
it creates no witness and permits no Begin. After any ambiguous response, a later
denial cannot prove that an earlier registration did not commit, so the parent
retains uncertainty and retries the original registration until positively resolved.
