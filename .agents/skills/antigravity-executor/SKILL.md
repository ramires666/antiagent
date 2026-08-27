---
name: antigravity-executor
description: Use the Antigravity/Gemini MCP executor in this repository for simple, bounded, verifiable coding tasks, lifecycle operations, setup, or troubleshooting. Do not use it for architecture, security, destructive work, secrets, commit, or push.
---

# Antigravity Executor

Use the configured `antigravity_cli_executor` MCP server. Keep Codex as the
orchestrator and Antigravity as a leaf coding worker.

## Route work

- Prefer the project-scoped `antigravity_worker` for a small task with clear
  files and acceptance criteria.
- If that custom agent is unavailable in the current session, call the
  lifecycle tools directly. If an older server exposes only the compatible
  one-shot tool, use `antigravity_cli_execute`.
- Do not delegate architecture, security, destructive operations, secrets,
  broad ambiguous refactors, Git commit, Git push, or final acceptance.
- Do not ask an Antigravity run to create another agent. Codex owns delegation.

## Run the lifecycle

1. Send only the minimum task context and never include credentials or secrets.
2. Start with `antigravity_agent_spawn` in `mode=plan`.
3. Use bounded `antigravity_agent_wait` calls and
   `antigravity_agent_status` until a terminal state is reached.
4. Use `antigravity_agent_followup` only for a terminal run with a conversation
   ID. Use `mode=accept-edits` only after explicit authorization for that exact
   edit; use `acknowledge_review=true` only after reviewing a previous partial
   or unknown result.
5. Interrupt work that is no longer needed with
   `antigravity_agent_interrupt`.
6. Independently inspect the structured result, `git diff`, changed files, and
   relevant tests. Gemini output is unverified until these checks pass.

On `capacity_reached`, provider usage limit, or provider unavailability, do not
repeat the same request. Fall back once to the cheapest suitable native worker
and report the fallback.

Read
[the executor guide](<../../../Инструкция_ Antigravity CLI OAuth Executor для Codex.md>)
when installing, configuring, troubleshooting, or checking the exact MCP
contract and security model.
