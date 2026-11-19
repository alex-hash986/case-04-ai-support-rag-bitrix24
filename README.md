# AI-поддержка и пресейл в Bitrix24

Ассистент первой линии поддержки и пресейла отвечает на типовые вопросы
по корпоративной базе знаний. Жалобы и обращения без надёжного источника
передаёт оператору с контекстом. Для пилота предусмотрены Открытые линии,
комментарии в CRM и учёт подтверждённых закрытий в SQLite.

На размеченном наборе из 60 диалогов каждый mock-вариант дал 60/60 верных
решений, 36/36 ожидаемых эскалаций и 0/24 неподтверждённых автоответов.
Статус проверок внешних подключений приведён в live-отчёте.

## Запуск на Windows

```powershell
py -m venv .venv
.venv/Scripts/python -m pip install -e '.[dev]'
if (!(Test-Path .env)) { Copy-Item .env.example .env }
.venv/Scripts/python -m scripts.start_all
```

Для раздельного запуска используйте `python -m src.api.main --mock-llm`
и `python -m scripts.run_ui` из виртуального окружения.

- [Live Demo и Executive ROI-дашборд](http://127.0.0.1:8504)
- [Контракты OpenAPI](http://127.0.0.1:8014/docs)
- [Проверка состояния](http://127.0.0.1:8014/health)

Доступны четыре быстрых сценария и собственные сообщения. Метрики строятся
по сохранённым решениям. Демо-тарифы и SLA заданы в `data/knowledge_base`.
Настройки моделей и бюджета описаны в `.env.example` и [регламенте](docs/operations.md).

## Пилот и материалы

- [Паспорт решения и формулы ROI](docs/passport.md)
- [Автономная презентация и калькулятор](docs/presentation.html)
- [Сценарий демонстрации](docs/playbooks/live-demo.md)
- Mock A/B ([Markdown](docs/benchmark_ab_report.md), [JSON](docs/benchmark_ab_report.json))
- Live preflight ([Markdown](docs/benchmark_live_report.md), [JSON](docs/benchmark_live_report.json))
- [Срез состояния](docs/status.md)

## Проверки

```powershell
.venv/Scripts/python -m pytest
.venv/Scripts/python -m ruff check .
.venv/Scripts/python -m mypy .
.venv/Scripts/python -m ops.runners.smoke
.venv/Scripts/python -m ops.runners.benchmark_ab --mock-llm
.venv/Scripts/python -m ops.runners.preflight_live
```
