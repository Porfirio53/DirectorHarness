from __future__ import annotations

from harnessbench.adapters import (
    CodexAdapter,
    DemoAdapter,
    FairyClawAdapter,
    GenericCliAdapter,
    MoltisAdapter,
    NullClawAdapter,
    NanoBotAdapter,
    NanoClawAdapter,
    OpenClawAdapter,
    PicoClawAdapter,
    ZeroClawAdapter,
    HermesAgentAdapter,
)
from harnessbench.adapters.base import BaseAdapter


def build_adapter(adapter_name: str) -> BaseAdapter:
    if adapter_name == "codex":
        return CodexAdapter()
    if adapter_name == "demo":
        return DemoAdapter()
    if adapter_name == "nanobot":
        return NanoBotAdapter()
    if adapter_name == "nanoclaw":
        return NanoClawAdapter()
    if adapter_name == "nullclaw":
        return NullClawAdapter()
    if adapter_name == "openclaw":
        return OpenClawAdapter()
    if adapter_name == "moltis":
        return MoltisAdapter()
    if adapter_name == "fairyclaw":
        return FairyClawAdapter()
    if adapter_name == "picoclaw":
        return PicoClawAdapter()
    if adapter_name == "zeroclaw":
        return ZeroClawAdapter()
    if adapter_name == "hermes_agent":
        return HermesAgentAdapter()
    if adapter_name == "generic_cli":
        return GenericCliAdapter()
    raise ValueError(f"unknown adapter: {adapter_name}")
