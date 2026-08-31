"""High-level JobPing business tools for the Codex application agent."""

__all__ = ["JobPingMCPServer"]


def __getattr__(name: str) -> object:
    """Lazily expose the facade without pre-importing the stdio module."""
    if name == "JobPingMCPServer":
        from app.mcp.server import JobPingMCPServer

        return JobPingMCPServer
    raise AttributeError(name)
