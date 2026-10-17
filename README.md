# dev-ai-stack

Личная инфраструктура для разработки на локальных моделях во всех проектах: один OpenAI-совместимый шлюз с псевдонимами моделей по ролям, общая память агентов через MCP и единые правила для разных инструментов.

Это инструмент разработки, а не продукт. Он не зависит от конкретного проекта: TaskFlow и другие репозитории подключаются к нему как клиенты.

## Статус

| Фаза | Что | Статус |
| --- | --- | --- |
| 1 | Настройка Ollama, Modelfile, `warmup.sh` | Готово |
| 2 | Postgres + pgvector, шлюз LiteLLM, Open WebUI | Готово, без облачного fallback |
| 3 | MCP-сервер памяти `devmem` | Не начато |
| 4 | Клиенты: Cursor, Continue, Roo Code, OpenCode / Aider | Не начато |
| 5 | Шаблон `AGENTS.md` и `bootstrap-project.sh` | Не начато |
| 6 | Бенчмарк моделей по ролям | Не начато |

## Железо

RTX 5070 Ti 16 ГБ, Ryzen 9 9950X3D, 59 ГБ ОЗУ, Ollama 0.31.1 как systemd-сервис на `127.0.0.1:11434`.

Рабочий стол GNOME использует видеопамять NVIDIA, поэтому аппаратное ускорение Chrome выключено. Запас видеопамяти при загруженной рабочей паре — около 1,5 ГБ.

## Модели и псевдонимы

| Псевдоним | Модель Ollama | Где живёт | Роль |
| --- | --- | --- | --- |
| `coder` | `qwen2.5-coder-14b-32k` | GPU, резидентная, 12 ГБ | Код, агент |
| `fast` | `qwen2.5-coder-3b-4k` | GPU, резидентная, 2,1 ГБ | Автодополнение |
| `reasoner` | `qwen3:14b` | GPU, по запросу | План, декомпозиция |
| `reviewer` | `gemma4:12b` | GPU, по запросу | Ревью |
| `vision` | `qwen3-vl:8b` | GPU, по запросу | Изображения |
| `embed` | `nomic-embed-text-cpu` | CPU | Эмбеддинги, 768 измерений |

Варианты моделей собираются из `ollama/modelfiles/`:

```bash
ollama create qwen2.5-coder-14b-32k -f ollama/modelfiles/coder-32k.Modelfile
ollama create qwen2.5-coder-3b-4k   -f ollama/modelfiles/fast-4k.Modelfile
ollama create nomic-embed-text-cpu  -f ollama/modelfiles/embed-cpu.Modelfile
```

## Прогрев моделей

`coder` должна загружаться раньше `fast`, иначе она может не влезть в видеопамять целиком и частично уйдёт на CPU.

```bash
./ollama/warmup.sh
```

Запускать перед работой и после фаз, где `reasoner` или `reviewer` вытеснили рабочую пару. Скрипт запускается как файл, не вставкой в терминал.

## Запуск стека

```bash
cp .env.example .env              # только на новой машине; секреты: openssl rand -hex 32
docker compose up -d              # postgres, litellm, open-webui
./litellm/issue-keys.sh           # выпустить виртуальные ключи клиентов в .env
docker compose up -d open-webui   # пересоздать, чтобы Open WebUI получил свой ключ
```

| Сервис | Адрес | Версия |
| --- | --- | --- |
| LiteLLM | `http://127.0.0.1:4000/v1`, админка `http://127.0.0.1:4000/ui` (вход по `LITELLM_MASTER_KEY`) | 1.105.1 |
| Open WebUI | `http://127.0.0.1:3001`, первый зарегистрированный пользователь — администратор | 0.12.3 |
| Postgres + pgvector | `127.0.0.1:5442`, базы `litellm` и `devmem` | 16.15, pgvector 0.8.7 |

Образы закреплены по digest в `docker-compose.yml`. Обновление — осознанное: поменять digest, поднять стек, прогнать проверки ниже.

### Виртуальные ключи

У каждого клиента свой ключ: в админке LiteLLM видно, кто сколько запросов сделал, и любой ключ можно отозвать, не трогая остальных. `issue-keys.sh` заполняет только пустые переменные, поэтому повторный запуск не меняет существующие ключи.

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
ollama ps   # coder и fast должны остаться Forever после запросов через шлюз
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

- `docs/DECISIONS.md` — журнал архитектурных решений.
- `docs/BEFORE_PUBLIC.md` — что проверить и убрать перед тем, как открыть репозиторий для портфолио.
- Полный план — `~/.cursor/plans/local_ai_dev_stack_95536ee7.plan.md`.
