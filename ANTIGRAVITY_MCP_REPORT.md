# Antigravity MCP: отчёт о сбоях и эксплуатационный план

## Executive summary

Antigravity несколько раз запускал generic CLI/diagnostic task и завершался
ошибкой. В отдельных попытках `accept-edits` оставлял частичный diff
(`research.py`, `test_research.py`), а в других попытках не оставлял изменений.
Это делает сам факт ошибки недостаточным для вывода о повреждении проекта.

Наиболее вероятная детерминированная проблема — неверная проверка Git-root в
рабочих каталогах, которые не были Git-репозиториями. Windows `TEMP`/`W:`
попытки также завершались отказом: даже временные каталоги с созданными и
закоммиченными тестовыми репозиториями не принимались. После инициализации и
commit исходного root проверка уже проходила. Это подтверждает важность
корректного root/preflight, но не доказывает, что dirty tree был причиной.

Наблюдение о shard из 220 строк означает успешно обработанный конкретный
shard, а не универсальный лимит Antigravity, CLI или MCP.

## Граница доказательств

В отчёте используются три статуса:

- **Observed** — непосредственно подтверждено выводом/результатом запуска.
- **Inferred** — правдоподобное объяснение, требующее проверки логами.
- **Unknown** — данных недостаточно.

Dirty tree — только корреляция во времени, не доказанная причина сбоя.

## Временная шкала

1. Запущен generic CLI diagnostic task через Antigravity.
2. Первый запуск завершился ошибкой на собственной диагностике.
3. Повторный запуск той же общей задачи также завершился ошибкой.
4. Появились проверки Git-root и попытки работы с Windows-путями `W:/Temp`.
5. Тестовые временные репозитории были созданы и закоммичены, но всё равно
   отклонялись проверкой Antigravity.
6. Исходный workspace после `git init` и commit стал проходить root validation.
7. В параллельных batches отдельные children не имели heartbeat; внешний
   `Promise.all` скрывал уже завершившиеся children до окончания самого
   медленного child.
8. В некоторых `accept-edits` попытках оставался частичный diff, в некоторых
   — не оставалось изменений.
9. Последующий fallback-исполнитель завершил исследовательский pipeline,
   подтвердив, что отказ Antigravity не был эквивалентен отказу проекта.

## Success/failure matrix

| Component | Observed result | Status | Interpretation |
|---|---|---|---|
| Generic CLI diagnostic, attempt 1 | Task failed | Observed FAIL | Причина скрыта generic error |
| Generic CLI diagnostic, attempt 2 | Task failed again | Observed FAIL | Повтор не изменил детерминированное условие |
| Git-root в исходном workspace до init | Root не распознавался | Observed FAIL | Workspace не удовлетворял Git precondition |
| Temporary `W:/Temp` repo | Даже committed test repo отклонён | Observed FAIL | Путь/контекст/validator требуют отдельной проверки |
| Исходный root после init + commit | Root validation прошёл | Observed SUCCESS | Validator зависит от корректного repo state |
| `safe.directory` | Использовался как часть диагностики | Observed | Не исправляет отсутствие `.git` |
| Sandbox/path access | Были Windows path attempts | Observed | Точный permission error не сохранён |
| Parallel batch execution | Child heartbeat отсутствует | Observed GAP | Нельзя видеть прогресс каждого child |
| Outer `Promise.all` | Completed child results скрыты до slowest | Observed GAP | Частичный успех не виден оператору |
| `accept-edits` output | Иногда partial diff, иногда none | Observed | Нужна обязательная postflight-проверка |
| Dirty tree | Совпадал по времени с отказами | Observed correlation | Причина не доказана |
| 220-line shard | Один shard завершился успешно | Observed SUCCESS | Не является общим лимитом |
| `conversation_id`/usage | В некоторых результатах отсутствовали | Observed GAP | Невозможна полная корреляция и cost audit |

## Exact reproduction

### A. Non-Git workspace

Запустить из исходного каталога до создания репозитория:

```powershell
Get-Location
Test-Path .git
git rev-parse --show-toplevel
```

Ожидаемое наблюдение: `.git` отсутствует, `git rev-parse` возвращает
`not a git repository`. Это должно классифицироваться как `workspace_not_git`,
а не как общий отказ diagnostic task.

### B. Temporary Windows repository

Создать изолированный test repo под разрешённым временным каталогом, выполнить
init и commit, затем передать его Antigravity как `cwd`. Зафиксировать отдельно:

- нормализованный абсолютный путь;
- наличие `.git`;
- root, возвращённый `git rev-parse`;
- exit code и stderr validator;
- sandbox decision.

Подтверждённый факт: committed repositories под `Windows TEMP/W:` всё равно
отклонялись в прошлых попытках. Не считать это доказательством одной причины
без stderr.

### C. Original root after initialization

Повторить ту же Git-проверку для исходного root после `git init` и commit.
Подтверждённый результат — проверка root прошла. Сравнить только окружение:

- exact path;
- drive/case/slash representation;
- owner/service account;
- sandbox profile;
- Git config;
- environment variables.

### D. Parallel children

Запустить один и тот же read-only diagnostic сначала последовательно, затем в
двух children параллельно. Для каждого child использовать собственный
`run_id` и каталог. Не использовать общий temp filename.

Проверить, что outer collector получает результат child сразу, а не только
после завершения slowest child. Задержку slowest child добавить только в
тестовом harness, не в production workspace.

### E. Partial edit behavior

До `accept-edits` сохранить только диагностические данные `git status --short`
и `git diff --` в checkpoint, если на это есть разрешение оператора. После
завершения снова снять status/diff и сравнить:

- no diff;
- complete diff;
- partial diff;
- generated/unexpected files.

Не выполнять автоматически `git checkout`, `git reset`, `git stash`, удаление
или перезапись файлов. При partial diff сначала остановиться, показать список
изменений и сохранить patch/commit checkpoint только с разрешением оператора.

## Hypotheses

### H1 — incorrect Git-root precondition (high confidence)

Validator требовал `.git` там, где исходный workspace его не имел. `safe.directory`
может разрешить trust issue существующего repo, но не создаёт Git-root. Нужно
явно поддержать non-Git workspace или выполнить Git init только как отдельное
разрешённое действие.

### H2 — Windows path normalization/sandbox mismatch (medium confidence)

`W:/Temp`, `W:\\Temp`, абсолютный путь после нормализации и путь service account
могли рассматриваться как разные каталоги. Отказы committed temporary repos
показывают, что одной проверки commit недостаточно.

### H3 — missing child observability (confirmed contributing factor)

Отсутствие per-child heartbeat и ожидание outer `Promise.all` не обязательно
ломают задачу, но скрывают частичный прогресс и задерживают диагностику.

### H4 — retrying deterministic failures (high confidence)

Повторение generic task без изменения root/preflight повторяет тот же отказ.
Retry следует применять только к transient network/timeout/5xx условиям.

### H5 — dirty tree as root cause (not established)

Изменённые файлы совпадали по времени с попытками, но это не доказывает, что
Antigravity отказал именно из-за dirty tree. Требуется validator stderr и
сравнение clean/dirty контрольных запусков.

## Fix plan

### P0

1. Ввести явный preflight `cwd_exists`, `is_git_repo`, `git_root`,
   `sandbox_access`, `tool_available`.
2. Разделить `workspace_not_git` и `git_trust_denied`; не путать их с CLI
   failure.
3. Передавать только нормализованный абсолютный `cwd`; логировать его безопасно.
4. Если workspace не Git, пропускать Git-only checks с состоянием `skipped`,
   если задача не требует Git явно.
5. Добавить per-child heartbeat и потоковую публикацию результатов.
6. Не повторять deterministic precondition failure.

### P1

1. Уникальные `run_id`, cwd и temp files для каждого child.
2. Collector на основе `Promise.allSettled` или эквивалентного механизма, чтобы
   completed/failed children были доступны независимо от slowest child.
3. Structured result для каждого child:

   ```json
   {
     "run_id": "...",
     "conversation_id": "...",
     "child_id": "...",
     "command": "...",
     "cwd": "...",
     "started_at": "...",
     "finished_at": "...",
     "heartbeat_at": "...",
     "exit_code": 0,
     "stdout": "...",
     "stderr": "...",
     "status": "success",
     "retry_count": 0,
     "usage": {}
   }
   ```

4. В `accept-edits` всегда выполнять postflight `git status`, diff summary и
   список новых файлов.
5. При partial diff блокировать дальнейшее редактирование до решения оператора.

### P2

1. Сохранять execution manifest с версиями CLI, MCP, ОС, Python/Node и sandbox.
2. Добавить clean/dirty и Git/non-Git матрицу в regression harness.
3. Добавить метрики длительности child, retry rate, hidden-completion count,
   missing-metadata count и размер stdout/stderr.
4. Хранить stderr и tool-call IDs с redaction секретов.

## Regression suite

Минимальный набор:

| Test | Expected |
|---|---|
| Existing Git root | Root detected |
| Non-Git workspace | Git checks skipped or typed `workspace_not_git` |
| Committed repo in allowed workspace | Accepted |
| Committed repo under Windows TEMP/W: | Explicit typed result, not generic failure |
| Clean tree | Diagnostic runs |
| Dirty tree | Diagnostic runs or explicit policy result; no unproven rejection |
| Missing cwd | `path_not_found` |
| Sandbox-denied path | `sandbox_denied` with path context |
| Two parallel children | Individual heartbeat/results visible |
| Slowest child | Earlier successes visible before it completes |
| Child timeout | Only timed-out child fails |
| Deterministic precondition failure | No blind retry |
| `accept-edits` no-op | Empty diff recorded |
| `accept-edits` complete edit | Full diff recorded |
| `accept-edits` partial edit | Partial state surfaced and workflow pauses |
| 220-line successful shard | Success recorded; no global-limit assertion |
| Missing conversation metadata | Warning/typed observability failure |

## Operator playbook

1. Перед запуском зафиксировать `pwd`, абсолютный `cwd`, sandbox profile и
   `git status --short`; не печатать `.env` values.
2. Проверить наличие `.git`, но не создавать/удалять репозиторий автоматически.
3. Запустить один маленький read-only smoke test последовательно.
4. Проверить structured result, stderr, exit code, conversation ID и usage.
5. Только после успешного smoke test запускать parallel batches.
6. Для каждого child контролировать heartbeat, run ID и отдельный output path.
7. При ошибке сохранить stdout/stderr и postflight status/diff.
8. При partial edit остановиться; не выполнять checkout/reset/stash и не удалять
   файлы автоматически.
9. Показать оператору exact diff и запросить разрешение на patch/commit
   checkpoint, если нужна точка восстановления.
10. Ретраить только после классификации ошибки как transient.
11. Если metadata отсутствует, пометить результат как incomplete observability,
   даже когда command exit code равен нулю.
12. После исправления повторить сначала последовательный smoke test, затем
   Git/non-Git, clean/dirty, Windows path и parallel regression matrix.

## Acceptance criteria

Сбой считается устранённым, когда:

- non-Git workspace даёт типизированный `skipped`/`workspace_not_git`;
- исходный Git-root и committed test root обрабатываются предсказуемо;
- Windows TEMP/W: результат объясняется собственным stderr/status;
- completed child виден до завершения slowest child;
- каждый child имеет heartbeat, IDs, exit code и usage либо явный missing-field;
- partial `accept-edits` не теряется и workflow безопасно останавливается;
- ни один автоматический recovery step не выполняет destructive Git/file action
  без явного разрешения оператора.
