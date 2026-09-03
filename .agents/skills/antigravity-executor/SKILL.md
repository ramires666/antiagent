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
4. When validating an installed launcher with `smoke_mcp.py`, treat any nonzero
   exit as a real failure. The smoke rejects an unhealthy doctor result and a
   stale MCP output schema even when all eight tool names are present; upgrade,
   fully restart Codex, and rerun it before trusting the session.

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
   A slow progress-notification consumer is bounded by the wrapper and must not
   extend the requested wait interval.
3. Continue with `antigravity_agent_followup` only after a terminal state with
   a saved conversation ID. A follow-up defaults back to `plan`.
4. Use `antigravity_agent_interrupt` when a live run is no longer wanted.
5. If an edit returns `review_required`, inspect the complete diff first. Set
   `acknowledge_review=true` only when deliberately continuing that reviewed
   partial/unknown result.

Use `antigravity_cli_execute` only as a compatibility fallback when durable
lifecycle tools are unavailable.

Workspace admission is durable and fair. `plan` requests `shared` access;
`accept-edits` requests `exclusive` access. Admission is queued before the
inter-process lock, reports `queue_position` and `blocking_owner_run_ids`,
and uses an owner/run-bound heartbeat lease. Shared readers may batch up to the
reader limit, while an earlier writer blocks later readers. Leases renew during
execution and are released on completion/cancel; expired leases are reconciled.

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

The wrapper preserves typed root-cause failures when bounded CLI diagnostics
are available. A soft-denied tool, profile/network failure, or structured
terminal error must not be interpreted as generic `invalid_json`, `no_content`,
or `timeout`. Intermediate NDJSON and diagnostic noise are drained with bounded
retained memory; only the final structured result is returned.

## Verify independently

Treat every result as untrusted. Check terminal `status`, `error_type`,
`manager_error`, `retryable`, `changed_paths`, `worktree_changed`,
`postflight_complete`, and `requires_review`; then inspect `git status`, the
complete relevant diff, and proportionate tests. Reject out-of-scope changes.

`progress_percent` measures only observable wrapper phases; it is not Gemini's
semantic completion percentage. `indeterminate=true` means no honest ETA is
available. Report the current phase, last safe activity, blocker and next action;
never infer an ETA from elapsed time. Telemetry deliberately excludes prompts,
context, argv, workspace paths, raw stdout/stderr, tool arguments and model
text. The declared `runtime.cli_executable` is the sole diagnostic path.

Content classification has five mutually exclusive codes:
`empty_model_response`, `stream_closed_before_final`, `content_parse_failed`,
`content_filtered`, and `final_block_missing`. Only
`stream_closed_before_final` and `final_block_missing` are retryable, and only
in `plan`; filtering, parsing, and empty output are not retryable. A later valid
final event takes precedence over malformed intermediate stream events.

Verification returns the expected-marker SHA-256 `rule_hash`, found/failure
fields, and a bounded sanitized `manual_review_content` suffix with
`manual_review_truncated`; raw prompts, tool events, credentials, and stderr
are never retained. Runtime output carries pre/post CLI identity and process
metadata; drift fails closed as `stale_runtime_snapshot`. Terminal
`finished_at`, `duration_seconds`, feedback `elapsed_seconds`, and
`idle_seconds` are frozen on the first finish and are not rewritten by later
polls or callbacks.

When Antiagent runtime or its lifecycle contract changes, update this skill,
the user guide, and `POST_UPDATE_ACTIVATION.md` in the same commit. A runtime
change with stale instructions is not complete. The post-update runbook is the
canonical operational handoff: close every Codex host, upgrade, start a new
top-level Codex process, then run doctor, strict installed-launcher smoke, and
one bounded live marker smoke.

For Windows install/upgrade, exact MCP contracts, and failure diagnosis, read
[the executor guide](<../../../Инструкция_ Antigravity CLI OAuth Executor для Codex.md>).
For the exact steps required after changing or upgrading Antiagent, read
[the post-update activation runbook](../../../POST_UPDATE_ACTIVATION.md).
Use `py -m antiagent_upgrade` only from the Antiagent checkout after fully
closing Codex; its process guard fails before `pipx` if MCP is still active.
