# Бэкенд «Цифрового механика»

## Локальный запуск

Нужны Python 3.10+ и Poppler для сборки поискового индекса. Моки заказчика и оригинальные PDF уже подготовлены в локальной рабочей копии. Для новой копии распакуйте [архив заказчика](https://disk.yandex.ru/d/QdAA8p4m0ySe8Q) так, чтобы `artifacts/customer/` содержал каталоги `mock_ml`, `mock_toir` и `data`. Шесть оригинальных PDF положите в `artifacts/manuals/`. В репозитории лежат исходные `data/vehicles.json`, `.env.example` и [контракты заказчика](../contracts/).

Из корня репозитория:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python -m manual_search.corpus artifacts/manuals artifacts/manual-corpus.json
```

В PowerShell из корня проекта:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m manual_search.corpus artifacts/manuals artifacts/manual-corpus.json
.\.venv\Scripts\python.exe main.py
```

Из корня проекта запустите все три сервиса одной командой:

```bash
.venv/bin/python main.py
```

В PyCharm можно открыть `main.py` и выбрать **Run 'main'** с интерпретатором из `.venv`. Скрипт проверит `/health` трёх сервисов и выведет ссылку на [Swagger UI](http://127.0.0.1:8000/docs). `Ctrl+C` останавливает все процессы запуска.

### Агент в чате

Для выбора инструмента и уточнения поискового запроса подключите запущенный локальный [Ollama](https://docs.ollama.com/api/chat). Пример запуска в PowerShell из корня проекта:

```powershell
ollama pull qwen2.5:3b
$env:OLLAMA_MODEL = "qwen2.5:3b"
.\.venv\Scripts\python.exe main.py
```

В Bash задайте модель через `export OLLAMA_MODEL=qwen2.5:3b`. Адрес и время ожидания можно поменять через `OLLAMA_URL` и `OLLAMA_TIMEOUT_SECONDS`. При пустом `OLLAMA_MODEL` или ошибке сервера работают поиск, контекст уточнений и команды «Получи результат диагностики», «Создай заявку». Технические данные и ссылки в ответ берутся из руководств выбранного двигателя. Чат создаёт черновик, а отправку в ТОиР подтверждает механик в форме.

### Запуск по отдельности

Если нужны собственные адреса ML и ТОиР, запускайте процессы отдельно. Mock ML и Mock ТОиР запускаются из корня в двух отдельных терминалах:

```bash
(cd artifacts/customer && ../../.venv/bin/python -m uvicorn mock_ml.app:app --port 8001)
```

```bash
(cd artifacts/customer && ../../.venv/bin/python -m uvicorn mock_toir.app:app --port 8002)
```

Бэкенд запускается в третьем терминале:

```bash
cp -n .env.example .env
set -a
. ./.env
set +a
.venv/bin/python -m uvicorn backend.app:app --port 8000
```

```bash
curl -fsS http://127.0.0.1:8001/health
curl -fsS http://127.0.0.1:8002/health
curl -fsS http://127.0.0.1:8000/health
```

OpenAPI бэкенда доступен по `http://127.0.0.1:8000/openapi.json`, Swagger UI — по `http://127.0.0.1:8000/docs`. Для фронтенда на отдельном локальном порту выставьте точный `FRONTEND_ORIGIN`, например `http://localhost:5173`. URL моков задаются через `MOCK_ML_URL` и `MOCK_TOIR_URL`. Можно переопределить `VEHICLES_PATH`, `MANUALS_DIR`, `MANUAL_CORPUS_PATH`, `MECHANIC_DB_PATH` и `UPSTREAM_TIMEOUT_SECONDS` (5 секунд по умолчанию). SQLite по умолчанию находится в `artifacts/mechanic.sqlite3`. Без собранного индекса поиск ограничен проверенными выдержками из репозитория.

## Маршруты

| Метод и путь | Назначение |
| --- | --- |
| `GET /api/manuals` | Каталог руководств с моделями двигателей, числом страниц и ссылками на доступные PDF. |
| `GET /api/vehicles` | Массив ТС. `vehicle_id` — госномер в исходном написании. `make`, `model`, `engine_model` всегда присутствуют, `model` может быть `null`. |
| `GET /api/vehicles/{vehicle_id}/sessions` | Массив с `session_id` и `checked_at` из каталога. |
| `POST /api/diagnose` | JSON с `vehicle_id` и `session_id`. Вызывает Mock ML, валидирует и сохраняет ответ. |
| `GET /api/diagnosis?vehicle_id=…&session_id=…` | Последний успешно полученный результат по паре из SQLite. |
| `POST /api/chat` | Пара ТС и сессии, `question` и необязательный массив `history` из шести последних пар `question` / `answer`. Ответ содержит `answer`, `needs_clarification`, `evidence` и, при вызове соответствующего инструмента, `diagnostic_result` или `draft`. |
| `POST /api/drafts` | JSON с парой ТС и сессии. Создаёт черновик по сохранённому отклонению или возвращает имеющийся черновик для того же кода. |
| `GET /api/drafts?vehicle_id=…&session_id=…` | Список черновиков и ранее отправленных заявок для восстановления экрана. |
| `GET /api/drafts/{draft_id}` | Актуальное состояние черновика и номер заявки, если известен. |
| `PATCH /api/drafts/{draft_id}` | Текущая `revision` и изменяемые поля из `conclusion`, `urgency`, `operations`, `spare_parts`, `references`. |
| `POST /api/drafts/{draft_id}/confirm` | JSON с текущей `revision`, `urgency` и `confirmed: true`. Отправляет заявку в Mock ТОиР. |
| `POST /api/drafts/{draft_id}/reconcile` | JSON с текущей `revision`, найденным `ticket_id` и `verified: true`. Сохраняет подтверждённый механиком номер при состоянии `delivery_unknown` и переводит черновик в `sent`. |

В каждом `evidence` есть `section`, `text` и `source` с `fragment_id`, `manual`, `pdf_page` и `url`. Ссылка относительна к адресу бэкенда и содержит `#page=N`. `url` равен `null`, если оригинальный PDF недоступен. В черновике и в запросе к ТОиР каждая `reference` содержит только `manual` и `pdf_page`. Все страницы нумеруются с единицы.

## Примеры для фронтенда

**Успешная диагностика, HTTP 200.** Запрос `{"vehicle_id":"E549YT198","session_id":"S-0010"}`.

```json
{
  "engine_model": "ЯМЗ-236М2",
  "diagnosis": {
    "vehicle_id": "E549YT198", "session_id": "S-0010", "status": "normal",
    "anomaly_type": null, "anomaly_title": null, "anomaly_score": 0.17, "confidence": 0.76,
    "signal_quality": "good", "description": "Признаков неисправности в записи не найдено",
    "model_version": "mock-v1"
  },
  "assessment": {
    "conclusion": "Значимых отклонений в записи не обнаружено.",
    "next_step": "При наличии жалоб сопоставить запись с наблюдениями механика.",
    "risk": null, "possible_causes": [], "checks": [], "repair_actions": [], "evidence": []
  }
}
```

**Сбой записи, HTTP 200.** Запрос `{"vehicle_id":"P456AT178","session_id":"S-0003"}`.

```json
{
  "engine_model": "ЯМЗ-236М2",
  "diagnosis": {
    "vehicle_id": "P456AT178", "session_id": "S-0003", "status": "check_failed",
    "anomaly_type": null, "anomaly_title": null, "anomaly_score": null, "confidence": null,
    "signal_quality": "bad", "description": "Запись отбракована: перегрузка микрофона",
    "model_version": "mock-v1"
  },
  "assessment": {
    "conclusion": null, "next_step": "Повторить запись двигателя.",
    "risk": null, "possible_causes": [], "checks": [], "repair_actions": [], "evidence": []
  }
}
```

**Черновик, HTTP 200.** После `POST /api/diagnose` с `E549YT198 / S-0011` передайте ту же пару в `POST /api/drafts`. Ниже пример после правки механиком.

```json
{
  "draft_id": "46be755d64ad4b9d830b940b4fb2e78d1", "revision": 2,
  "vehicle_id": "E549YT198", "session_id": "S-0011", "anomaly_type": "F02",
  "state": "draft", "ticket_id": null, "delivery_error": null, "urgency": null,
  "conclusion": "Предполагаемое низкое давление масла. Требуется замер.",
  "operations": ["Проверить давление на прогретом двигателе."],
  "spare_parts": [],
  "references": [{"manual": "236-3902150-Б РЭ", "pdf_page": 19}]
}
```

**Успешное подтверждение, HTTP 200.** Передайте `{"revision":2,"urgency":"high","confirmed":true}` в `POST /api/drafts/{draft_id}/confirm`. Ответ содержит весь черновик с обновлёнными полями:

```json
{"state":"sent","ticket_id":"TKT-0001","urgency":"high","delivery_error":null}
```

**Ошибка доставки, HTTP 503.** При недоступном ТОиР API возвращает код ошибки и весь сохранённый черновик. Ниже поля, по которым фронтенд определяет результат:

```json
{
  "error": {"code": "toir_unavailable", "message": "Нет соединения: сервис ТОиР."},
  "draft": {
    "draft_id": "46be755d64ad4b9d830b940b4fb2e78d1", "revision": 2,
    "state": "delivery_failed", "ticket_id": null, "urgency": "high",
    "delivery_error": {"code": "toir_unavailable", "message": "Нет соединения: сервис ТОиР."}
  }
}
```

### Пустые поля и обработка состояний

- В диагностике все ключи обязательны. Для `normal` и `check_failed` тип и название аномалии равны `null`. Для `check_failed` оценки и заключение также равны `null`. `description`, `model_version` и `signal_quality` есть при любом исходе.
- До подтверждения вывод и приоритет могут быть `null`, а массивы работ, источников и запчастей могут быть пустыми. Для отправки обязательны непустые `conclusion`, `operations`, `references` и приоритет `low`, `medium` или `high`. Запчасти без определения передаются как `[]`. Код аномалии и пару ТС–сессия задаёт сервер.
- При `sending` показывайте ожидание и опрашивайте `GET /api/drafts/{draft_id}`. Повторное подтверждение `sent` возвращает тот же `ticket_id`. Изменение черновика повышает `revision`, устаревшая правка или подтверждение дают HTTP 409.
- При `delivery_failed` черновик можно повторно подтвердить после устранения ошибки. При `delivery_unknown` механик находит заявку в ТОиР по ТС, сессии и коду отклонения и сохраняет подтверждённый номер через `POST /api/drafts/{draft_id}/reconcile`. Номер хранится в SQLite. Повтор с тем же номером идемпотентен, другой номер после сверки возвращает HTTP 409. Автоматическая отправка при неизвестном результате заблокирована, поскольку ТОиР мог создать заявку до разрыва связи.
- В новых черновиках для 69 отклонений из набора заказчика работы и источники заполняются по проверенным страницам. При спорной комплектации предлагается сверить маркировку и фактический состав узла. Для сохранённых черновиков с пропусками можно взять работы и источники из `assessment` последней диагностики и сохранить их через `PATCH /api/drafts/{draft_id}`. Диагностические работы, запчасти и приоритет проверяет механик.
- Клиент может передать до шести предыдущих успешных обменов по выбранной паре ТС и сессии в поле `history`.
- Ошибки локальной проверки пары и ML, в том числе HTTP 404, 422, 503 и 504, возвращают `error.code` и `error.message`. Показывайте ошибку операции. `check_failed` — самостоятельный результат только из валидного ответа ML.

### Приёмка

Проверьте `POST /api/diagnose` с тремя парами:

| Госномер | Сессия | Ожидаемый статус |
| --- | --- | --- |
| `E549YT198` | `S-0010` | `normal` |
| `E549YT198` | `S-0011` | `anomaly` |
| `P456AT178` | `S-0003` | `check_failed` |

Создайте черновик для `S-0011`, подтвердите и повторите `GET /api/drafts/{draft_id}` в Swagger UI. Номер должен сохраниться после обновления страницы и перезапуска бэкенда. Для воспроизведения сбоя остановите Mock ТОиР перед подтверждением ещё не отправленного черновика. Подтверждение вернёт HTTP 503, повторный GET покажет сохранённый черновик.

```bash
python -m unittest discover -s tests -p test_backend.py -v
PYTHONPATH=. python tests/check_manual_search.py artifacts/manual-corpus.json
```

Интеграционные тесты исполняют оригинальные ASGI-приложения заказчика. Инъекции ошибок транспорта покрывают и случай, когда заявка уже принята, а клиент не получил ответ из-за тайм-аута.
