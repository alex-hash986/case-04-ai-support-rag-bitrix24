# Фаза 3. Проверка live-подключений

Дата проверки — 20 ноября 2026. [Машиночитаемый вывод preflight](benchmark_live_report.json)

| Проверка | Наблюдение |
|---|---|
| OpenAI-совместимый каталог | Доступен. FAST, SMART и EMBEDDING найдены |
| Тарифы | Ставки входа и выхода LLM заданы. Ставка входа эмбеддингов отсутствует |
| Лимиты | 1,00 ₽ на цикл и 5,00 ₽ на сессию, валидация пройдена |
| Bitrix24 | Первичный `ConnectTimeout` при запросе `scope`. Повторный запрос прошёл |
| Методы CRM | `crm.timeline.comment.add` и `crm.timeline.comment.list` доступны по `method.get` |
| Методы Открытых линий | `imbot.message.add`, `imopenlines.bot.session.operator` и `imopenlines.session.history.get` недоступны |
| Регистрация бота | `BITRIX_BOT_ID`, `BITRIX_CLIENT_ID` и `BITRIX_APP_TOKEN` отсутствуют |

Проверочные вызовы моделей и A/B в режиме live заблокированы отсутствующей
ставкой эмбеддингов. Action Accuracy, Escalation Recall, Grounding Pass Rate,
Hallucination Rate, p50/p95 и средняя стоимость live-цикла остаются без замера.
Локальный Ollama на порту 11434 недоступен в этом окружении.

Для сквозного теста требуются права бота, реквизиты его регистрации и
выделенные тестовые чат и сделка. Записи в портал не выполнялись. Удалённые
ID операций и проверка reconcile на портале ожидают этих условий.
[Результаты локального mock A/B](benchmark_ab_report.md)
