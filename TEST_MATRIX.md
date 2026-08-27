# MCP test matrix

Актуально для baseline `cd41f44` (27 августа 2026 г.). Автоматические тесты offline, кроме отдельно отмеченного authenticated live smoke.

## MCP protocol — 5 тестов

| Сценарий | Ожидаемый результат |
|---|---|
| STDIO initialize и `tools/list` | Согласованный MCP и ровно один documented tool |
| Schema и valid call | `task` обязателен, defaults `thinking_level=medium`, `mode=plan`; structured output валиден |
| Unknown/missing/wrong/invalid arguments | Без падения process, без запуска Git/CLI и без утечки входных данных |
| Runtime error | MCP `isError=true`, structured metadata сохранена, stderr/secrets redacted |
| Progress/cancellation/sequential call | `run_id` и state видимы, cleanup завершён, следующая операция работает |

## Input/workspace

Проверяются пустые/нестроковые `task`, `context`, `verification`, некорректные enum и prompt limits; пустой cwd использует process cwd. Проверяются Git root, вложенный root, non-Git, missing path, выход за allowed root, symlink/junction и dirty workspace. `accept-edits` дополнительно проверяется на clean/dirty/no-op/complete/partial state и review acknowledgement.

## Process and security

Проверяются executable resolution, exact argv, отсутствие shell и dangerous permission bypass, safe child environment, spawn/OSError, timeout, cancellation, bounded stdout/stderr, reader failure, Windows Job Object/exact PID tree kill и POSIX fallback. Output не включает raw stderr, prompt, environment или secrets; truncation — head + marker + tail.

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
5. Authenticated live OAuth `plan` smoke и isolated `accept-edits` smoke успешны; временный repo удалён.
6. Финальный filename-only tracked-secret scan: `0` high-confidence matches и `0` подозрительных tracked-имён.
