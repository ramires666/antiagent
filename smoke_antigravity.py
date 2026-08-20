import asyncio

from google.antigravity import Agent, BuiltinTools, CapabilitiesConfig, LocalAgentConfig
from google.antigravity.hooks import policy

from server import build_model_target, get_workspace, require_gemini_api_key


SMOKE_ALLOWED_TOOLS = (
    BuiltinTools.LIST_DIR,
    BuiltinTools.FIND_FILE,
    BuiltinTools.SEARCH_DIR,
    BuiltinTools.VIEW_FILE,
    BuiltinTools.FINISH,
)


async def main() -> None:
    workspace = get_workspace()
    api_key = require_gemini_api_key()
    config = LocalAgentConfig(
        system_instructions="Inspect repository access. Do not modify anything.",
        capabilities=CapabilitiesConfig(
            enabled_tools=list(SMOKE_ALLOWED_TOOLS), enable_subagents=False
        ),
        workspaces=[str(workspace)],
        policies=[
            policy.deny_all(),
            *[policy.allow(tool.value) for tool in SMOKE_ALLOWED_TOOLS],
        ],
        model=build_model_target("medium", api_key=api_key),
    )
    async with Agent(config) as agent:
        response = await agent.chat(
            "Describe this repository and list up to five important files."
        )
        print(await response.text())


if __name__ == "__main__":
    asyncio.run(main())
