# Эксплуатация

Установите проект в .venv командой из README. Убедитесь, что 8014 и 8504
доступны на 127.0.0.1. Запустите FastAPI и дождитесь ответа GET /health.
Для демо запускайте API с --mock-llm. Демо выполняется командой
`python -m ops.runners.smoke`. Для чистого прогона задайте отдельный DATABASE_PATH.

Для локального Ollama используйте BASE_URL=http://127.0.0.1:11434/v1,
выберите установленные модели FAST, SMART и EMBEDDING и задайте ставки
в рублях за миллион токенов. Для неметрируемого сервера можно задать нули.
Переключите LLM_MODE=live, вызовите POST /api/v1/kb/reindex и проверьте
index_ready. Используйте один экземпляр API на файл базы.

При pending вызовите POST /api/v1/support/reconcile. Ответ покажет число
успешно восстановленных и ещё ожидающих решений. Таблица bitrix_steps
содержит методы, статусы и ошибки. Файл SQLite содержит тексты обращений,
контекст и логи, поэтому ограничьте к нему доступ.

## Бенчмарк Фазы 2

`python -m ops.runners.benchmark_ab --mock-llm` сравнивает оба варианта в
изолированной SQLite и сохраняет JSON и Markdown в docs. Отчёт
отображается в Executive ROI-дашборде. Задержка замеряется без сети
Bitrix24 и индексации.

## Live preflight и бенчмарк

```powershell
python -m ops.runners.preflight_live
python -m ops.runners.benchmark_ab --live-llm --output docs/benchmark_live_report.json
```

Запускайте вторую команду при `llm_ready: true`. Preflight читает каталог
моделей и проверяет ставки и лимиты. При готовой конфигурации он выполняет
три коротких запроса с бюджетной предавторизацией через LLM Gateway.
Каждый сценарий бенчмарка также проходит предавторизацию. Лимиты относятся
к циклу и сессии, поэтому совокупный расход серии из двух вариантов и
затраты на индексацию учитывайте при согласовании бюджета всего прогона.

Проверка Bitrix24 использует `scope` и `method.get` для методов публикации
и сверки. Доступ к конкретному чату или сделке проверяется в интеграционном
сценарии. [Документация method.get](https://apidocs.bitrix24.com/api-reference/common/system/method-get.html)

Preflight сообщает статусы LLM и Bitrix24 независимо. Код выхода 1
означает, что хотя бы одно из подключений требует внимания. Для выгрузки
диагностики добавьте `--output <путь.json>`. Вывод содержит только статусы,
лимиты и учтённую стоимость проверочных вызовов.

## Портал Bitrix24

Задайте BITRIX_MODE=live, HTTPS-адрес BITRIX_WEBHOOK_URL без имени метода,
BITRIX_BOT_ID зарегистрированного бота, BITRIX_CLIENT_ID из регистрации бота
и BITRIX_APP_TOKEN приложения.
Вебхук должен иметь доступ к imbot, imopenlines и crm. Очередь операторов
определяется настройкой Открытой линии на портале. Зарегистрируйте для бота
обработчик ONIMBOTMESSAGEADD на /api/v1/bitrix/openlines/webhook. При внешнем
доступе направляйте этот путь на локальный API через HTTPS-прокси.

Сверка подтверждает запись по метке в истории чата или таймлайне сделки.
Для неизвестного исхода перевода либо недоступной истории проверьте портал
вручную. Состояние sending сохраняет потенциально выполненную операцию.
Явные ограничения Bitrix и ошибки подключения допускают повторную отправку.

Для приёмки используйте выделенные тестовые чат Открытых линий и сделку.
Через API отправьте типовой вопрос и затем отдельное обращение с запросом
оператора. Передайте реальные тестовые `dialog_id` и `deal_id`. Для каждого
запроса сохраните ключ идемпотентности. Проверьте сообщение, комментарий и
перевод на портале. Повторите запросы с прежними ключами, вызовите
`POST /api/v1/support/reconcile` и сопоставьте квитанции `bitrix_steps` с
удалёнными ID. Количество записей по меткам должно сохраниться. Зафиксируйте
подтверждённые ID и результат проверки дублей.

Задайте BASELINE_MANUAL_MINUTES по результатам ручного замера. Доверенный
источник подтверждения закрытия вызывает /api/v1/support/confirm-closure с
request_id последнего ответа и заголовком X-Bitrix-App-Token. До подключения
источника подтверждений минуты остаются пустыми.

[Отправка сообщения бота](https://apidocs.bitrix24.com/api-reference/chat-bots/outdated/messages/imbot-message-add.html) ·
[Событие ONIMBOTMESSAGEADD](https://apidocs.bitrix24.com/api-reference/chat-bots/outdated/messages/events/on-imbot-message-add.html) ·
[Перевод на оператора](https://apidocs.bitrix24.com/api-reference/imopenlines/openlines/chat-bots/imopenlines-bot-session-operator.html) ·
[История OpenLines](https://apidocs.bitrix24.com/api-reference/imopenlines/openlines/sessions/imopenlines-session-history-get.html) ·
[Список комментариев CRM](https://apidocs.bitrix24.com/api-reference/crm/timeline/comments/crm-timeline-comment-list.html) ·
[Настройка вебхуков](https://helpdesk.bitrix24.ru/open/20886106/)
