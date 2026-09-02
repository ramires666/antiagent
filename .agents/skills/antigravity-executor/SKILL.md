---
name: antigravity-executor
description: Use the Antigravity/Gemini MCP executor for bounded, verifiable coding, documentation, test, setup, or troubleshooting leaf work. Do not use it for architecture, security decisions, destructive work, secrets, commit, push, or final acceptance.
---

# Antigravity Executor

Use `antigravity_cli_executor` before a native worker for suitable leaf work.
Codex remains responsible for decomposition, permissions, integration, review,
tests, commit, push, and the final answer. There is no artificial call-count
limit: run as many useful independent, non-overlapping tasks as provider and
manager capacity allow.

## Establish readiness

1. Call `antigravity_doctor` with `working_directory=""`. This inherits the
   current Codex Git-root. Doctor checks the CLI, declared host boundary,
   wrapper state, and Git workspace; `oauth_ready=unknown` and
   `network_probe=not_run` are expected and are not a live provider check.
2. After install, upgrade, registration, or a `program not found` failure,
   fully restart the top-level Codex CLI/app/IDE process. A new subagent inside
   an old session still has the old MCP snapshot.
3. Confirm OAuth and network only with one bounded live `mode=plan` smoke using
   an `expected_marker`, then verify `status=SUCCESS`, the marker, and unchanged
   Git state.

## Prepare a run

- Give one concrete outcome, exact relative paths, preserved invariants, and
  observable verification. Never include credentials, tokens, private keys,
  passwords, cookies, keyring data, or unrelated file contents.
- The public contract supports only `payload_mode=workspace`; omit the field or
  use `workspace`. Current `agy` has no strict file allowlist or deny-shell, so
  `file_scope_enforced=false` and `shell_denied=false` are honest limitations.
- For small headless analysis, place the minimum already-inspected, non-secret
  facts in `context` and say: "Use only CONTEXT; do not call read_file or shell;
  if insufficient, return NEEDS_CONTEXT with relative paths." Relative
  `@file` references are best effort, not an access boundary.
- Use `low` for lookup, `medium` for ordinary implementation/test work, and
  `high` for ambiguous cross-file debugging. Choose the needed level directly.

## Run the lifecycle

1. Dispatch independent scopes with `antigravity_agent_spawn`. Start a new
   conversation in `mode=plan`; use `mode=accept-edits` only for a specifically
   authorized edit scope.
2. Save every `agent_id`. Use `antigravity_agent_wait` for at most 60 seconds.
   A wait timeout does not cancel the run; inspect `antigravity_agent_status`
   before retrying or replacing it.
   Read and relay the returned `progress` object on every state change: `phase`,
   wrapper-based `progress_percent`, `recent_events`, `blocker`, `next_action`,
   `elapsed_seconds`, `idle_seconds`, and `manager_status`. During a long wait,
   keep using bounded waits so MCP progress notifications and heartbeats reach
   the parent instead of leaving it without status.
3. Continue with `antigravity_agent_followup` only after a terminal state with
   a saved conversation ID. A follow-up defaults back to `plan`.
4. Use `antigravity_agent_interrupt` when a live run is no longer wanted.
5. If an edit returns `review_required`, inspect the complete diff first. Set
   `acknowledge_review=true` only when deliberately continuing that reviewed
   partial/unknown result.

Use `antigravity_cli_execute` only as a compatibility fallback when durable
lifecycle tools are unavailable.

## Retry and fallback

- `capacity_reached`: wait for an existing run to finish; do not create a retry
  storm.
- Wait timeout: check status and keep waiting or interrupt; do not duplicate a
  run that is still active.
- One stalled/disconnected instance or thread-local failure: preserve useful
  output, inspect Git, narrow the unfinished scope, and try two or three fresh
  instances when safe.
- Confirmed provider-wide quota/rate limit: make at most one justified fresh
  attempt, then use the cheapest suitable native worker.
- `permission_denied`: retry only after changing the payload to sufficient
  inline context or switch to an interactive host run/native worker.
- Auth, profile, network-policy, path-policy, stale snapshot, unsupported
  payload, and repeated deterministic failures are not fixed by fresh agents.

Never use `--dangerously-skip-permissions`.

## Verify independently

Treat every result as untrusted. Check terminal `status`, `error_type`,
`manager_error`, `retryable`, `changed_paths`, `worktree_changed`,
`postflight_complete`, and `requires_review`; then inspect `git status`, the
complete relevant diff, and proportionate tests. Reject out-of-scope changes.

`progress_percent` measures only observable wrapper phases; it is not Gemini's
semantic completion percentage. `indeterminate=true` means no honest ETA is
available. Report the current phase, last safe activity, blocker and next action;
never infer an ETA from elapsed time. Telemetry deliberately excludes prompts,
context, argv, paths, raw stdout/stderr, tool arguments and model text.

When Antiagent runtime or its lifecycle contract changes, update this skill and
the user guide in the same commit. A runtime change with stale instructions is
not complete.

For Windows install/upgrade, exact MCP contracts, and failure diagnosis, read
[the executor guide](<../../../Инструкция_ Antigravity CLI OAuth Executor для Codex.md>).
Use `py -m antiagent_upgrade` only from the Antiagent checkout after fully
closing Codex; its process guard fails before `pipx` if MCP is still active.
