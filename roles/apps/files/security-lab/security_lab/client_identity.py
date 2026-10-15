"""Host-authenticated client identity, never an MCP tool argument."""

from contextvars import ContextVar


delegate_client: ContextVar[str] = ContextVar("security_lab_delegate", default="codex")
