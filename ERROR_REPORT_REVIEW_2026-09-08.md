# Проверка отчёта использования Antigravity

Дополнение после аудита: отдельный queue budget реализован и проверен;
актуальный статус этапов — в [WORK_PLAN.md](WORK_PLAN.md). Ниже сохранён
снимок выводов до реализации этого исправления.

Проверено 8 сентября 2026. Источник:
`C:/projects/HARDDEV/smartgold/IPC/Dev/antigravity-errors-report-2026-09-03.md`,
включая дополнения от 8 сентября. Исходный файл не изменён и не отправлялся
Antigravity: исполнителям передавались только задания на чтение кода Antiagent.

## Вывод

Реально предотвратимы длительное непрозрачное ожидание очереди, повторные
бесполезные попытки, усечение диагностических списков и работа со старой
установкой под видом новой. Потерю ответа можно устранить в parser только
тогда, когда CLI действительно передал ответ в распознаваемом виде. Ненулевой
usage этого не доказывает. Гарантировать отсутствие provider/CLI failures нельзя.

В исходном снимке 22 lock timeout + 18 no_content = 40 из 46 ошибок (87%).
Это исторический агрегат 83 записей; поздние волны к нему не прибавляются.

## Что уже реализовано

| Ошибка из отчёта | Проверенное состояние исходников | Ограничение |
| --- | --- | --- |
| Непрозрачная блокировка | `agy_server.py:580` admission: shared plan, exclusive edits, очередь, blockers, lease; `agent_manager.py:474` fairness/expiry | Отдельного короткого queue timeout нет; browser-enabled plan обоснованно exclusive |
| Stale/cancelled admission | Release в finally, expiry в store, тесты отмены и crash OS lock | Сбой renew не прекращает работу явно; см. ниже |
| Всё превращается в no_content | `response_diagnostics.py:199`: пять различных content failures | Классификация не возвращает отсутствующий текст |
| Terminal response пропущен | `agy_server.py:2154`: response → typed content → agent_response.text_delta, с ограничением памяти | Нужен успешный terminal event; thinking/tool output не используется как ответ |
| Verification без причины | `agy_server.py:1220`: rule hash/name, found, failure_kind, sanitized suffix | Marker mismatch по-прежнему должен оставаться ошибкой |
| Смена CLI во время сессии | `runtime_identity.py:226`: pre/post identity, process guard; `smoke_mcp.py:82` strict schema | Doctor не сравнивает checkout с установленными исходниками; binary identity stat-based, не hash содержимого exe |

## Реальные оставшиеся изменения по приоритету

### P0 — прекратить бесконечные повторы missing final

Три ограниченных read-only проверки текущего аудита завершились
`final_block_missing`: agents `4346bad78a824ade868026daa79d8824`,
`08a7b2951e3942a590f21f949b3fc038`, `a89146bd42ad4bd2966a142dde318347`.
Для первого дополнительно проверено: final event есть, malformed=0,
terminal blocks=0, answer deltas=0, response_source=null, postflight=true,
changed_paths=[], worktree_changed=false. Это повторение симптома, а не
доказательство его конкретной upstream-причины.

Нужны два независимых изменения:

1. Безопасная структурная диагностика: счётчики типов событий/answer steps,
   категории типа response/content, наличие полей, размеры, неизвестные формы
   только как bounded категории. Не сохранять raw prompt/tool payload/stderr.
   На публичной синтетической задаче сопоставить поток и terminal envelope;
   добавить fixture обнаруженной формы и только после этого расширять parser.
2. Ограничивать повторы на уровне логической задачи, а не agent_id: максимум
   один осмысленно суженный retry для plan, затем native fallback. Команда
   «продолжи» сама по себе не должна сбрасывать историю одинакового сбоя.
   Новый пробный запуск оправдан изменением CLI, настроек или новым scope.
   Edit/browser runs автоматически не повторять из-за возможных побочных эффектов.

Acceptance: обнаруженная поддерживаемая форма восстанавливается fixture-тестом;
неподдерживаемая остаётся типизированной ошибкой; N одинаковых повторных запросов
не вызывают N provider runs. Для этого потребуется явный идентификатор логической
задачи либо состояние оркестратора; совпадение свободного prompt не является
надёжным контрактом дедупликации.

### P1 — разделить очередь и исполнение

`admitted_workspace` получает общий deadline; `lock.acquire(remaining)` также
может ждать весь остаток. `AgentStore.create` (`agent_manager.py:628`) считает
queued и running в одном лимите 32. CLI запускается после admission, поэтому
ожидающие задачи не тратят provider tokens, но занимают manager capacity.

Добавить настраиваемый queue budget, например 30–60 секунд, отдельно от
execution deadline; в ошибке сохранять очередь и владельцев. Разделить лимит
ожидающих и выполняющихся задач с ограничением обеих очередей и fairness.
Внешний OS lock без записи admission показывать как неизвестного владельца,
не приписывать его случайному agent ID.

Acceptance: удерживаемый writer не заставляет читателя ждать 840 секунд;
отмена ожидающего не запускает CLI; освобождение writer пропускает корректного
следующего владельца; очередь не исчерпывает все execution slots.

### P1 — обновлять lease-телеметрию и обрабатывать потерю lease

`_renew_workspace_admission` (`agy_server.py:566`) обновляет БД, но не передаёт
новый snapshot в lifecycle. При queued состоянии уведомление отправляется только
при изменении позиции/владельцев; общий heartbeat сохраняет старый
workspace_admission. Поэтому показанный lease_expires_at может быть устаревшим,
хотя запись в БД продлевается. Ошибка renew лишь логируется либо приводит к return.

Публиковать свежий snapshot при продлении и явно завершать/отменять выполнение
при подтверждённой потере admission. OS lock продолжает защищать конфликтующие
доступы: по текущему коду нельзя утверждать, что этот дефект уже вызывает
одновременную запись. Проблема доказана в наблюдаемости и реакции на отказ.

Acceptance: fake-clock тест видит свежие heartbeat/expiry в status, а потеря lease
не оставляет run выглядеть здоровым до общего timeout.

### P2 — компактные списки и воспроизводимые проверки

`AgentStore.list` (`agent_manager.py:686`) возвращает полные snapshots с output;
`_snapshot_output` сохраняет output. Ограничение числа записей не ограничивает
суммарный размер ответа. Добавить summary/default без текстов ответа и отдельный
detail/status, cursor pagination и счётчики ошибок. Проверять общий byte budget.

`test_current_failures.py:222` делает 100 вызовов `_success_result` с подготовленным
payload. Это полезный unit-тест, но не 100 CLI/provider smoke. Отдельный opt-in
soak-runner должен выполнять ограниченные реальные marker runs, останавливаться
после малого числа failures и сохранять компактную статистику. Даже 100 успешных
запусков не дают гарантии нулевой вероятности повторения ошибки.

## Что не следует «исправлять» обходом

- Approval denial до spawn/status — внешний слой. Не создавать fake failed
  manager record и не считать denied status доказательством провала durable run.
- Не отключать verification ради зелёного результата.
- Не считать exit_code=0 или usage>0 доказательством содержательного успеха.
- Не сохранять сырой приватный envelope ради диагностики; regex-redaction не
  гарантирует очистку произвольного project content.
- Не считать смену thinking level решением: исторические сбои были на всех
  уровнях; текущий контракт разрешает только high.

## Фактическая проверка и следующий шаг

Root запустил `.venv/Scripts/python.exe -m unittest test_current_failures
test_response_diagnostics test_runtime_identity test_smoke_mcp -q`: 41 тест, OK.
Проверка verification дополнительно сверена дешёвым native агентом; два других
native audit получили внешний usage limit, их незавершённые выводы не приняты
как доказательство. Все ключевые выводы выше проверены root непосредственно.

Свежий doctor: CLI 1.1.27, установленный wrapper 0.4.1/schema 3, checks_passed=true,
drift_reasons=[]. Checkout: 0.5.0/schema 4. Это разные версии; стабильность старой
установки не доказывает активацию новых исходников. Сначала выполнить handoff по
POST_UPDATE_ACTIVATION.md, затем один bounded live marker smoke.

Рекомендуемый порядок дальнейшей разработки: диагностика финального ответа и
ограничение повторов → отдельный queue budget → lease telemetry/loss handling →
summary/pagination → opt-in live soak. Этот документ — результат аудита и план
проверяемых изменений; перечисленные доработки в этом аудите не внедрялись.
