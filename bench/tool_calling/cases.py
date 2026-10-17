"""Agent-style tool-calling cases. Tools are never executed."""

from __future__ import annotations

SYSTEM = (
    "Ты кодовый агент в локальном репозитории. "
    "Инструменты: read_file(path), write_file(path, content), list_dir(path), "
    "run_command(command), search(query). "
    "Если нужны файл, каталог, команда или поиск по коду — вызови ровно один инструмент "
    "и дождись результата, затем при необходимости вызови следующий. "
    "Не вызывай несколько инструментов в одном ответе. "
    "Не выдумывай содержимое файлов и вывод команд. "
    "Если вопрос общий или код уже есть в сообщении пользователя и инструменты не нужны — "
    "ответь обычным текстом и не вызывай инструменты."
)

TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a UTF-8 text file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path, absolute or relative to the workspace.",
                    }
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write a UTF-8 text file, replacing existing contents.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to write."},
                    "content": {"type": "string", "description": "Full new file contents."},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List entries in a directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path."}
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a shell command and return its combined output.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Command line to run."}
                },
                "required": ["command"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search",
            "description": "Search the workspace for a text query.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Literal text to search for."}
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
]

TOOL_NAMES = {tool["function"]["name"] for tool in TOOLS}

# expect: one dict per turn. Keys are argument names, or "<field>_contains"
# when the value (or each item of a list) must occur inside that string field.
# tool_result is the synthetic role:tool payload injected after turn 1.
CASES: list[dict] = [
    {
        "id": "single_read_file",
        "name": "Read one source file",
        "category": "single",
        "user": "Прочитай файл src/app.py.",
        "expect": [{"tool": "read_file", "args": {"path": "src/app.py"}}],
    },
    {
        "id": "single_list_dir",
        "name": "List one directory",
        "category": "single",
        "user": "Покажи содержимое каталога bench.",
        "expect": [{"tool": "list_dir", "args": {"path": "bench"}}],
    },
    {
        "id": "single_run_command",
        "name": "Run pytest",
        "category": "single",
        "user": "Запусти команду pytest -q и ничего больше.",
        "expect": [{"tool": "run_command", "args": {"command": "pytest -q"}}],
    },
    {
        "id": "single_search",
        "name": "Search for a symbol",
        "category": "single",
        "user": "Найди в кодовой базе текст memory_save.",
        "expect": [{"tool": "search", "args": {"query": "memory_save"}}],
    },
    {
        "id": "single_write_file",
        "name": "Write a note file",
        "category": "single",
        "user": "Запиши в файл notes/todo.txt ровно такой текст: проверить тесты",
        "expect": [
            {
                "tool": "write_file",
                "args": {"path": "notes/todo.txt", "content": "проверить тесты"},
            }
        ],
    },
    {
        "id": "cyrillic_read_path",
        "name": "Read a Cyrillic path",
        "category": "cyrillic",
        "user": "Прочитай файл /home/user/Проект/файл.py.",
        "expect": [
            {"tool": "read_file", "args": {"path": "/home/user/Проект/файл.py"}}
        ],
    },
    {
        "id": "cyrillic_search",
        "name": "Search for a Cyrillic name",
        "category": "cyrillic",
        "user": "Найди в коде текст счётчик_запросов.",
        "expect": [{"tool": "search", "args": {"query": "счётчик_запросов"}}],
    },
    {
        "id": "cyrillic_list_dir",
        "name": "List a Cyrillic directory",
        "category": "cyrillic",
        "user": "Покажи содержимое каталога /home/user/Документы/Отчёты.",
        "expect": [
            {"tool": "list_dir", "args": {"path": "/home/user/Документы/Отчёты"}}
        ],
    },
    {
        "id": "chain_list_then_read",
        "name": "List a directory, then read main.py",
        "category": "chain",
        "user": (
            "Узнай, какие файлы лежат в каталоге src. "
            "Когда получишь список, прочитай файл src/main.py."
        ),
        "tool_result": "main.py\nutils.py\n",
        "expect": [
            {"tool": "list_dir", "args": {"path": "src"}},
            {"tool": "read_file", "args": {"path": "src/main.py"}},
        ],
    },
    {
        "id": "chain_search_then_read",
        "name": "Search for warmup, then open the file",
        "category": "chain",
        "user": (
            "Найди в коде текст warmup. "
            "Когда поиск вернёт путь к файлу, прочитай этот файл."
        ),
        "tool_result": "ollama/warmup.sh:15:load()\n",
        "expect": [
            {"tool": "search", "args": {"query": "warmup"}},
            {"tool": "read_file", "args": {"path": "ollama/warmup.sh"}},
        ],
    },
    {
        "id": "chain_read_then_write",
        "name": "Read a config, then write the fixed copy",
        "category": "chain",
        "user": (
            "Прочитай файл config.yaml. Когда получишь содержимое, запиши в "
            "config.fixed.yaml тот же текст, но со значением timeout 60 вместо 30."
        ),
        "tool_result": "timeout: 30\nhost: 127.0.0.1\n",
        "expect": [
            {"tool": "read_file", "args": {"path": "config.yaml"}},
            {
                "tool": "write_file",
                "args": {
                    "path": "config.fixed.yaml",
                    "content_contains": ["timeout: 60", "127.0.0.1"],
                },
            },
        ],
    },
    {
        "id": "chain_run_then_search",
        "name": "Run tests, then search for the failure",
        "category": "chain",
        "user": (
            "Запусти тесты командой pytest -q. Когда увидишь результат, "
            "если тесты упали с AssertionError, найди в коде текст AssertionError."
        ),
        "tool_result": "1 failed\ntests/test_api.py:10: AssertionError\n",
        "expect": [
            {"tool": "run_command", "args": {"command": "pytest -q"}},
            {"tool": "search", "args": {"query": "AssertionError"}},
        ],
    },
    {
        "id": "no_tool_recursion",
        "name": "Explain recursion without tools",
        "category": "no_tool",
        "user": (
            "Объясни в двух предложениях, что такое рекурсия. "
            "Файлы, команды и поиск не нужны."
        ),
        "expect": [],
    },
    {
        "id": "no_tool_snippet",
        "name": "Explain an inline snippet",
        "category": "no_tool",
        "user": (
            "Объясни, что делает этот код. Файл открывать не нужно, код уже ниже.\n\n"
            "def add(a, b):\n"
            "    return a + b"
        ),
        "expect": [],
    },
    {
        "id": "no_tool_describe_only",
        "name": "Describe a tool call without making it",
        "category": "no_tool",
        "user": (
            "Представь, что нужно прочитать README.md. Опиши словами, какой "
            "инструмент подошёл бы и с каким аргументом path. Сам инструмент не "
            "вызывай: это вопрос про формат, а не просьба читать файл."
        ),
        "expect": [],
    },
]
