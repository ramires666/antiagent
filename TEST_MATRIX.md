# MCP test matrix

## Браузер — дополнение 0.5.0 (8 сентября 2026)

| Сценарий | Проверка |
| --- | --- |
| Browser mode MCP contract | Три режима, default disabled у execute/spawn/followup; invalid types до preflight |
| Дочерний процесс | Режим явно передаётся в env; родительский opt-in не наследуется |
| Admission | Browser-enabled plan получает exclusive workspace access |
| User session | Межпроцессная блокировка; взаимное исключение и release на временном тестовом профиле путей |
| Proxy | Whitelist, блокировки evaluation/network/initScript/file paths/non-HTTP URL; unit + real MCP rejection smoke |
| Disabled | Реальный MCP tools/list возвращает пустой список без Node/Chrome |
| Isolated live | Локальная страница, уникальный маркер через snapshot, закрытие вкладки; пройдено с backend 1.8.0 |
| User session live | Не выполнено: требуется предоставление пользовательской сессии и Chrome consent |
| Installed full chain | После restart/upgrade, регистрации bridge и точечных permissions по POST_UPDATE_ACTIVATION.md |

Ниже сохранена матрица базовой реализации.

Актуально для Antiagent `0.4.1` (3 сентября 2026 г.). Автоматические тесты offline, кроме отдельно отмеченного authenticated live smoke.

## MCP protocol — 6 тестов

| Сценарий | Ожидаемый результат |
|---|---|
| STDIO initialize и `tools/list` | Согласованный MCP и восемь documented tools |
| Schema и valid call | `task` обязателен, единственный/default `thinking_level=high`, `mode=plan`, единственный `payload_mode=workspace`; structured output валиден |
| Unknown/missing/wrong/invalid arguments | Без падения process, без запуска Git/CLI и без утечки входных данных |
| Runtime error | MCP `isError=true`, structured metadata сохранена, stderr/secrets redacted |
| Progress/cancellation/sequential call | Числовая шкала `0..100`, safe phase/activity, cleanup завершён, следующая операция работает |
| Persistent lifecycle | `spawn/wait/status/list/followup/interrupt`, terminal output, lineage, durable progress и отсутствие prompt в snapshot |

## Persistent agent manager

| Сценарий | Ожидаемый результат |
|---|---|
| Новый `AgentStore` на той же БД | Terminal snapshot/result восстановлен |
| `queued → running → terminal` | Только разрешённые условные transitions; terminal immutable |
| Immediate interrupt до первого task step | Запись `interrupted`, task registry очищен |
| Cancel через второе SQLite connection | Running executor замечает flag, отменяется и освобождает workspace lock |
| Follow-up после `accept-edits` | Conversation продолжена, но новый безопасный default снова `plan` |
| Stale `queued|running` | `failed`, `manager_error=manager_lost` |
| Durable progress | Migration старой БД; monotonic wrapper percent; heartbeat обновляет liveness; terminal immutable |
| Event ring | Не более 16 allowlisted событий; heartbeat coalescing; free-form telemetry и секреты отвергаются |
| Capacity/history/output bounds | 32 active, 1000 terminal, 256 KiB result; безопасные typed errors |
| SQLite schema | `progress_json` мигрируется additively; нет `task`, `context`, `verification`; `owner_id` и DB path не выдаются snapshot’ом |
| 32 plan одного workspace | Все 32 одновременно доходят до mock CLI через shared admission; lock timeout отсутствует |
| Writer fairness / stale lease | Ранний writer блокирует поздних readers; истёкший owner удаляется атомарно |
| Interrupt в очереди | Admission waiter отменяется и durable ticket освобождается |

## Input/workspace

Проверяются пустые/нестроковые `task`, `context`, `verification`, некорректные enum и prompt limits; пустой cwd использует process cwd. Проверяются Git root, вложенный root, non-Git, missing path, выход за allowed root, symlink/junction и dirty workspace. `accept-edits` дополнительно проверяется на clean/dirty/no-op/complete/partial state и review acknowledgement.

## Process and security

Проверяются executable resolution, exact argv с `--model gemini-3.8-flash-high`, `--effort high` и `--output-format stream-json`, high-only input schema, отсутствие shell и dangerous permission bypass, safe child environment, spawn/OSError, timeout, cancellation, bounded stdout/stderr, reader failure, Windows Job Object/exact PID tree kill и POSIX fallback. NDJSON parser отдаёт terminal response либо bounded recovery только из `agent_response.text_delta`; thinking/tool deltas, raw event envelopes, raw stderr, prompt, environment и secrets не попадают в diagnostics/progress.

Отдельно проверяются пять content-кодов, 100 последовательных marker-results,
safe verification suffix/hash, pre/post runtime identity/version drift с
`stale_runtime_snapshot` и неизменность terminal elapsed/idle counters.

## Git postflight

| Сценарий | Ожидаемый результат |
|---|---|
| Clean `accept-edits` no-op | `worktree_changed=false`, postflight complete |
| Existing dirty tree | Не отклоняется только из-за dirty; `preexisting_dirty=true` |
| Successful edit | Только фактически изменённые paths, complete postflight |
| Timeout/cancellation с изменениями, partial/unknown | `requires_review=true`, no reset/stash/rollback |
| Persistent marker | Следующий editing call требует `acknowledge_review=true` |

## Release acceptance

1. `unittest discover` проходит дважды и в обратном порядке targeted suites.
2. Real STDIO protocol suite проходит.
3. `py_compile`, `pip check`, `git diff --check` проходят.
4. Проверены Windows lifecycle, lock, clean/dirty/postflight и отсутствие surviving children.
5. Исторические authenticated OAuth `plan` и isolated `accept-edits` smoke успешны. Повторный managed smoke подтвердил terminal lifecycle и неизменный Git, но внешний provider вернул usage limit; повтор не выполнялся.
6. Финальный filename-only tracked-secret scan: `0` high-confidence matches и `0` подозрительных tracked-имён.
