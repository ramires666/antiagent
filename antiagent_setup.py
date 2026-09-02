from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Mapping, Sequence


SERVER_NAME = "antigravity_cli_executor"
BOUNDARY_ENV = "ANTIAGENT_EXECUTION_BOUNDARY=host"


class SetupError(RuntimeError):
    """Raised when a safe, portable Codex registration cannot be prepared."""


def _first_existing_file(candidates: Sequence[Path]) -> Path | None:
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if resolved.is_file():
            return resolved
    return None


def resolve_mcp_launcher(
    explicit: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    argv0: str | None = None,
) -> Path:
    env = os.environ if environ is None else environ
    executable_name = "antiagent-mcp.exe" if os.name == "nt" else "antiagent-mcp"
    candidates: list[Path] = []

    if explicit:
        resolved = _first_existing_file([Path(explicit)])
        if resolved is None:
            raise SetupError("Explicit antiagent-mcp launcher does not exist")
        return resolved
    configured = env.get("ANTIAGENT_MCP_PATH")
    if configured:
        candidates.append(Path(configured))

    invoked_as = Path(sys.argv[0] if argv0 is None else argv0)
    if invoked_as.name.lower().startswith("antiagent-"):
        candidates.append(invoked_as.parent / executable_name)

    pipx_bin = env.get("PIPX_BIN_DIR")
    if pipx_bin:
        candidates.append(Path(pipx_bin) / executable_name)
    user_profile = env.get("USERPROFILE") or env.get("HOME")
    if user_profile:
        candidates.append(Path(user_profile) / ".local" / "bin" / executable_name)
    discovered = shutil.which("antiagent-mcp", path=env.get("PATH"))
    if discovered:
        candidates.append(Path(discovered))

    resolved = _first_existing_file(candidates)
    if resolved is None:
        raise SetupError(
            "antiagent-mcp launcher not found. Install/update the package with "
            "'py -m pipx install --force .' or pass --launcher <absolute-path>."
        )
    if not resolved.is_absolute():
        raise SetupError("Resolved antiagent-mcp launcher is not absolute")
    return resolved


def resolve_codex_cli(
    explicit: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> Path:
    env = os.environ if environ is None else environ
    candidates: list[Path] = []
    if explicit:
        resolved = _first_existing_file([Path(explicit)])
        if resolved is None:
            raise SetupError("Explicit Codex CLI path does not exist")
        return resolved
    configured = env.get("CODEX_CLI_PATH")
    if configured:
        candidates.append(Path(configured))
    local_app_data = env.get("LOCALAPPDATA")
    if local_app_data:
        codex_bin = Path(local_app_data) / "OpenAI" / "Codex" / "bin"
        try:
            installed = sorted(
                codex_bin.glob("*/codex.exe"),
                key=lambda path: path.stat().st_mtime_ns,
                reverse=True,
            )
        except OSError:
            installed = []
        candidates.extend(installed)
    for name in ("codex.cmd", "codex.exe", "codex"):
        discovered = shutil.which(name, path=env.get("PATH"))
        if discovered:
            candidates.append(Path(discovered))

    resolved = _first_existing_file(candidates)
    if resolved is None:
        raise SetupError(
            "Codex CLI not found. Pass --codex <absolute-path> or set CODEX_CLI_PATH."
        )
    if not resolved.is_absolute():
        raise SetupError("Resolved Codex CLI path is not absolute")
    return resolved


def registration_commands(codex: Path, launcher: Path) -> tuple[list[str], list[str], list[str]]:
    return (
        [str(codex), "mcp", "remove", SERVER_NAME],
        [
            str(codex),
            "mcp",
            "add",
            SERVER_NAME,
            "--env",
            BOUNDARY_ENV,
            "--",
            str(launcher),
        ],
        [str(codex), "mcp", "get", SERVER_NAME],
    )


def register(codex: Path, launcher: Path, *, dry_run: bool = False) -> None:
    remove_command, add_command, get_command = registration_commands(codex, launcher)
    if dry_run:
        print(subprocess.list2cmdline(remove_command))
        print(subprocess.list2cmdline(add_command))
        print(subprocess.list2cmdline(get_command))
        return

    removed = subprocess.run(remove_command, text=True, capture_output=True, check=False)
    if removed.returncode != 0 and "not found" not in (removed.stderr + removed.stdout).lower():
        raise SetupError(f"Unable to remove prior MCP registration (exit {removed.returncode})")

    added = subprocess.run(add_command, text=True, capture_output=True, check=False)
    if added.returncode != 0:
        raise SetupError(f"Unable to add MCP registration (exit {added.returncode})")

    verified = subprocess.run(get_command, text=True, capture_output=True, check=False)
    if verified.returncode != 0:
        raise SetupError(f"Unable to verify MCP registration (exit {verified.returncode})")
    normalized_output = (verified.stdout + verified.stderr).replace("\\\\", "\\").lower()
    if str(launcher).lower() not in normalized_output:
        raise SetupError("Codex registration does not contain the resolved absolute launcher path")

    print(f"Registered {SERVER_NAME}")
    print(f"Launcher: {launcher}")
    print("Restart Codex completely, then run antigravity_doctor and a read-only plan smoke.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Register Antiagent in Codex with a resolved absolute pipx launcher."
    )
    parser.add_argument("--launcher", help="Explicit absolute antiagent-mcp launcher path")
    parser.add_argument("--codex", help="Explicit absolute Codex CLI path")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the safe registration commands without changing Codex config",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        launcher = resolve_mcp_launcher(args.launcher)
        codex = resolve_codex_cli(args.codex)
        register(codex, launcher, dry_run=args.dry_run)
    except SetupError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
