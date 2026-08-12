from __future__ import annotations

from abc import ABC, abstractmethod

from harnessbench.models import AdapterRunContext, AdapterRunResult


class BaseAdapter(ABC):
    name = "base"

    @abstractmethod
    def run(self, ctx: AdapterRunContext) -> AdapterRunResult:
        raise NotImplementedError

