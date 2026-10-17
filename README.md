# dev-ai-stack

Личная инфраструктура для разработки на локальных моделях во всех проектах: один OpenAI-совместимый шлюз с псевдонимами моделей по ролям, общая память агентов через MCP и единые правила для разных инструментов.

Это инструмент разработки, а не продукт. Он не зависит от конкретного проекта: TaskFlow и другие репозитории подключаются к нему как клиенты.

## Статус

| Фаза | Что | Статус |
| --- | --- | --- |
| 1 | Настройка Ollama, Modelfile, `warmup.sh` | Готово |
| 2 | Postgres + pgvector, шлюз LiteLLM, Open WebUI | Готово, без облачного fallback |
| 3 | MCP-сервер памяти `devmem` | Готово, systemd-юнит ставится вручную |
| 4 | Клиенты: Cursor, Continue, Roo Code, OpenCode / Aider | Не начато |
| 5 | Шаблон `AGENTS.md` и `bootstrap-project.sh` | Не начато |
| 6 | Бенчмарк моделей по ролям | Не начато |

## Железо

RTX 5070 Ti 16 ГБ, Ryzen 9 9950X3D, 59 ГБ ОЗУ, Ollama 0.31.1 как systemd-сервис на `127.0.0.1:11434`.

Рабочий стол GNOME использует видеопамять NVIDIA, поэтому аппаратное ускорение Chrome выключено. Запас видеопамяти при загруженной рабочей паре — около 1,5 ГБ.

## Модели и псевдонимы

| Псевдоним | Модель Ollama | Где живёт | Роль |
| --- | --- | --- | --- |
| `coder` | `qwen2.5-coder-14b-32k` | GPU, резидентная, 12 ГБ | Код и правки без инструментов |
| `agent` | `qwen3-14b-32k` | GPU, резидентная, 11 ГБ | Агенты с tool calling |
| `fast` | `qwen2.5-coder-3b-4k` | GPU, резидентная, 2,1 ГБ | Автодополнение |
| `reasoner` | `qwen3:14b` | GPU, по запросу | План, декомпозиция |
| `reviewer` | `gemma4:12b` | GPU, по запросу | Ревью |
| `vision` | `qwen3-vl:8b` | GPU, по запросу | Изображения |
| `embed` | `nomic-embed-text-cpu` | CPU | Эмбеддинги, 768 измерений |

`coder` и `agent` резидентны по очереди: вместе с `fast` в 16 ГБ помещается только одна большая модель.

Варианты моделей собираются из `ollama/modelfiles/`:

```bash
ollama create qwen2.5-coder-14b-32k -f ollama/modelfiles/coder-32k.Modelfile
ollama create qwen3-14b-32k         -f ollama/modelfiles/qwen3-14b-32k.Modelfile
ollama create qwen2.5-coder-3b-4k   -f ollama/modelfiles/fast-4k.Modelfile
ollama create nomic-embed-text-cpu  -f ollama/modelfiles/embed-cpu.Modelfile
```

## Прогрев моделей

Большая модель профиля загружается раньше `fast`: Ollama выбирает размещение один раз при загрузке. `coder` и `agent` вместе с `fast` в 16 ГБ не помещаются, в памяти только одна такая пара.

```bash
./ollama/warmup.sh                 # coder, затем fast
./ollama/warmup.sh agent           # qwen3-14b-32k, затем fast
./ollama/warmup.sh --force-gpu     # fast с num_gpu=99
```

Если `fast` после загрузки не на 100% GPU, скрипт выгружает её и пробует ещё раз. `--force-gpu` заставляет все слои `fast` на GPU и может завершиться ошибкой, если видеопамяти действительно не хватает.

| Код | Значение |
| --- | --- |
| 0 | обе модели 100% GPU |
| 1 | ошибка загрузки |
| 2 | модели загружены, но хотя бы одна не целиком на GPU |

Запускать перед работой, после смены профиля и после фаз, где `reasoner` или `reviewer` вытеснили рабочую пару. Скрипт запускается как файл, не вставкой в терминал.

## Запуск стека

```bash
cp .env.example .env              # только на новой машине; секреты: openssl rand -hex 32
docker compose up -d              # postgres, litellm, open-webui
./litellm/issue-keys.sh           # выпустить виртуальные ключи клиентов в .env
./litellm/sync-key-models.sh      # обновить список моделей у уже выпущенных ключей
docker compose up -d open-webui   # пересоздать, чтобы Open WebUI получил свой ключ
```

| Сервис | Адрес | Версия |
| --- | --- | --- |
| LiteLLM | `http://127.0.0.1:4000/v1`, админка `http://127.0.0.1:4000/ui` (вход по `LITELLM_MASTER_KEY`) | 1.105.1 |
| Open WebUI | `http://127.0.0.1:3001`, первый зарегистрированный пользователь — администратор | 0.12.3 |
| Postgres + pgvector | `127.0.0.1:5442`, базы `litellm`, `devmem` и `devmem_test` | 16.15, pgvector 0.8.7 |

Образы закреплены по digest в `docker-compose.yml`. Обновление — осознанное: поменять digest, поднять стек, прогнать проверки ниже.

### Виртуальные ключи

У каждого клиента свой ключ: в админке LiteLLM видно, кто сколько запросов сделал, и любой ключ можно отозвать, не трогая остальных. `issue-keys.sh` заполняет только пустые переменные, поэтому повторный запуск не меняет существующие ключи. Уже выпущенным ключам список моделей обновляет `./litellm/sync-key-models.sh`: ключ не ротируется, повторный запуск ничего не меняет, если список уже совпадает. Полный список — `coder`, `fast`, `reasoner`, `reviewer`, `vision`, `embed`, `agent`. Ключ devmem остаётся только на `embed`.

| Переменная | Клиент | Доступные модели |
| --- | --- | --- |
| `LITELLM_OPENWEBUI_KEY` | Open WebUI | все |
| `LITELLM_CONTINUE_KEY` | Continue | все |
| `LITELLM_ROO_KEY` | Roo Code | все |
| `LITELLM_OPENCODE_KEY` | OpenCode / Aider | все |
| `LITELLM_TASKFLOW_KEY` | TaskFlow | все |
| `LITELLM_DEVMEM_KEY` | devmem | только `embed` |

### Проверки

```bash
set -a; . ./.env; set +a
curl -s 127.0.0.1:4000/v1/models -H "Authorization: Bearer $LITELLM_CONTINUE_KEY"
curl -s 127.0.0.1:4000/v1/chat/completions -H "Authorization: Bearer $LITELLM_CONTINUE_KEY" \
  -H 'Content-Type: application/json' -d '{"model":"coder","messages":[{"role":"user","content":"Say OK"}]}'
curl -s 127.0.0.1:4000/v1/embeddings -H "Authorization: Bearer $LITELLM_DEVMEM_KEY" \
  -H 'Content-Type: application/json' -d '{"model":"embed","input":"test"}'
ollama ps   # активный профиль (coder или agent) и fast должны остаться Forever
```

### Облачный fallback

Пока выключен: нет ключа внешнего провайдера. Чтобы включить — заполнить `OPENROUTER_API_KEY` в `.env`, раскомментировать `coder-cloud` и `fallbacks` в `litellm/config.yaml`, добавить `coder-cloud` в список моделей ключей и выполнить `docker compose up -d litellm`.

## Настройки Ollama

Копия systemd-оверрайда лежит в `ollama/systemd-override.conf`. Применить на новой машине:

```bash
sudo systemctl edit ollama   # вставить содержимое файла
sudo systemctl daemon-reload && sudo systemctl restart ollama
systemctl show ollama -p Environment | tr ' ' '\n' | grep OLLAMA_
```

## Память devmem

MCP-сервер общей памяти агентов: решения, итоги шагов, уроки, задачи и handoff между сессиями, клиентами и моделями. Streamable HTTP на `http://127.0.0.1:8765/mcp`, каждый запрос с `Authorization: Bearer $DEVMEM_TOKEN`. Данные — база `devmem` в Postgres, эмбеддинги — через шлюз (`embed`, ключ `LITELLM_DEVMEM_KEY`), не напрямую в Ollama.

Память — контекст, а не доказательство. Каждый ответ поиска и бриф начинается с `context, verify against code`, у записи есть `commit_sha`.

### Установка и запуск

```bash
cd devmem
uv sync --extra dev               # Python 3.12, зависимости из uv.lock
.venv/bin/devmem migrate          # схема в базе из DEVMEM_DATABASE_URL, повторный запуск ничего не делает
.venv/bin/devmem serve            # 127.0.0.1:8765/mcp; --stdio — запасной режим
```

Настройки читаются из `.env` в корне репозитория, переменные окружения их перекрывают. Если в shell осталась `DEVMEM_DATABASE_URL` от тестов, команды пойдут в другую базу: проверяйте строку `on database ...` у `migrate`.

systemd-юнит (`devmem/systemd/devmem.service`) ставится от своего пользователя, не от root:

```bash
mkdir -p ~/.config/systemd/user
cp devmem/systemd/devmem.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now devmem
systemctl --user status devmem
ss -ltn | grep 8765               # должен быть только 127.0.0.1:8765
```

### Инструменты MCP

| Инструмент | Что делает |
| --- | --- |
| `memory_start_session(project, task_title?)` | Регистрирует проект, возвращает бриф: последний handoff, открытые задачи, решения, уроки; около 1500 токенов |
| `memory_search(query, project, kinds?, task_id?, k=8)` | Гибридный поиск: полнотекст `simple` и вектор, слияние RRF; `superseded` исключены |
| `memory_save(project, kind, content, task_id?, refs?, commit_sha?)` | Запись в зарегистрированный проект, секреты отклоняются |
| `memory_supersede(entry_id, new_content, reason)` | Старая запись помечается заменённой, история сохраняется |
| `task_create`, `task_update`, `task_list` | Задачи: `open`, `in_progress`, `done`, `dropped` |
| `memory_handoff(task_id, summary, next_steps, open_questions)` | Итог при смене модели или конце сессии |

Ресурс: `devmem://project/{slug}/brief`. Формат ответа — `DEVMEM_RESPONSE_FORMAT` (`json` по умолчанию, `toon`, `csv`) или параметр `fmt` у инструментов.

Поле `client` у записи заполняется из заголовка `X-Devmem-Client`, поле `model` — из `X-Devmem-Model`. Сервер работает без сессий, поэтому `clientInfo` из `initialize` до вызовов инструментов не доходит: клиенту нужно передавать заголовки в конфиге (фаза 4).

### CLI

```bash
D=devmem/.venv/bin/devmem
$D project add --slug taskflow --root ~/Projects
$D import --project taskflow --path research/LESSONS_LEARNED.md --kind lesson   # повторный импорт не плодит дубли
$D import --project taskflow --glob ".cursor/plans/*.md" --kind imported
$D search --project taskflow --query "PostgreSQL слой для RAG logs" --k 5 [--fmt toon]
$D export --project taskflow > devmem/exports/taskflow.jsonl                     # каталог в .gitignore
$D reindex [--project taskflow]                                                  # после смены модели эмбеддингов
```

Импорт режет Markdown по заголовкам. Путь файла внутри корня проекта хранится относительным, хеш считается от пути и текста.

### Проверки

```bash
cd devmem && uv run pytest -q     # unit + интеграционные на devmem_test
set -a; . ../.env; set +a
npx -y @modelcontextprotocol/inspector --cli http://127.0.0.1:8765/mcp --transport http \
  --method tools/list --header "Authorization: Bearer $DEVMEM_TOKEN"
```

Интеграционные тесты работают только с базой, имя которой оканчивается на `_test` (по умолчанию `devmem_test`, переопределяется `DEVMEM_TEST_DATABASE_URL`). Создать её один раз:

```bash
docker compose exec -T postgres psql -U devai -d postgres -c 'CREATE DATABASE devmem_test'
docker compose exec -T postgres psql -U devai -d devmem_test -c 'CREATE EXTENSION IF NOT EXISTS vector'
```

## Порты

Всё слушает только `127.0.0.1`.

| Порт | Сервис |
| --- | --- |
| 11434 | Ollama |
| 4000 | LiteLLM |
| 5442 | Postgres + pgvector |
| 8765 | devmem MCP |
| 3001 | Open WebUI |

Порты TaskFlow, которые нельзя занимать: 3000, 5432, 5433, 5678, 6333, 6334, 6379, 8000, 8002, 8080, 9000, 9001, 9090, 18081, 18082, 19092.

## Секреты

Шаблон — `.env.example`, рабочий `.env` в `.gitignore`. Ключи внешних API и токены в репозиторий не попадают.

## Документы

- `docs/DECISIONS.md` — почему сделано так: решения с обоснованием.
- `docs/JOURNAL.md` — лабораторный журнал: что пробовали, замеры, сбои, выводы.
- `docs/BEFORE_PUBLIC.md` — что проверить и убрать перед тем, как открыть репозиторий для портфолио.
- Полный план — `~/.cursor/plans/local_ai_dev_stack_95536ee7.plan.md`.
