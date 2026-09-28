from orchestrator.tools.base import Tool, ToolContext, ToolError, ToolResult
from orchestrator.tools.builtin import ALL_TOOLS
from orchestrator.tools.router import ROLE_TOOLS, ToolOutcome, ToolRegistry, ToolRouter


def default_registry() -> ToolRegistry:
    return ToolRegistry(ALL_TOOLS)


__all__ = [
    "ALL_TOOLS",
    "ROLE_TOOLS",
    "Tool",
    "ToolContext",
    "ToolError",
    "ToolOutcome",
    "ToolRegistry",
    "ToolResult",
    "ToolRouter",
    "default_registry",
]
