"""Provider-neutral request/response types for the LLM gateway."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ToolCall(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    parameters: dict[str, Any]


class Message(BaseModel):
    model_config = ConfigDict(frozen=True)

    role: Literal["system", "user", "assistant", "tool"]
    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    # Provider-native content blocks of an assistant turn (e.g. Claude's thinking + tool_use
    # blocks), replayed unchanged to the provider that produced them.
    raw_content: tuple[dict[str, Any], ...] | None = None


class LLMRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    node: str
    messages: tuple[Message, ...]
    tools: tuple[ToolSpec, ...] = ()
    # JSON schema for structured output (used by the supervisor and reviewer verdicts).
    response_schema: dict[str, Any] | None = None
    # Never sent to a real provider. The fake LLM reads it to behave deterministically.
    metadata: dict[str, Any] = Field(default_factory=dict)


class Usage(BaseModel):
    model_config = ConfigDict(frozen=True)

    input_tokens: int = Field(ge=0)  # uncached input
    output_tokens: int = Field(ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)

    @property
    def total(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )


class LLMResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    usage: Usage
    model: str
    provider: str
    raw_content: tuple[dict[str, Any], ...] | None = None


class ProviderError(Exception):
    """Any provider failure. Triggers fallback to the secondary route."""


class RateLimitError(ProviderError):
    """HTTP 429. Retried with exponential backoff before falling back."""

    def __init__(self, message: str = "rate limited", retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class ProviderTimeout(ProviderError):
    pass


class InvalidModelOutput(ProviderError):
    """The provider refused the model's generated output (bad JSON or tool call).

    Retried on the same route a few times. Usage is not reported on this path, so the
    exception carries a worst-case estimate that the gateway charges.
    """

    def __init__(self, code: str, usage: Usage) -> None:
        super().__init__(code)
        self.code = code
        self.usage = usage
