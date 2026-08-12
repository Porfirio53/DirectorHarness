"""封装编剧 Harness 可用的大模型接口。

当前仅保留真实 OpenAI-compatible 路径，用于保证“是否启用 writer_harness”
这一实验变量本身不会再被 mock 推理能力干扰。
"""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod


class LLMClient(ABC):
    """编剧 Harness 所依赖的大模型客户端抽象，方便替换不同 API 后端。"""

    @abstractmethod
    def complete(self, system_prompt: str, user_prompt: str) -> str:
        raise NotImplementedError


class OpenAICompatibleClient(LLMClient):
    """OpenAI-compatible 模型接口，适配 DeepSeek、Qwen、OpenRouter、Ollama 网关等。"""

    def __init__(self, model: str, base_url: str | None = None, api_key: str | None = None, temperature: float = 0.2):
        self.model = model
        self.base_url = base_url or os.getenv("OPENAI_BASE_URL")
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.temperature = temperature

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        """调用真实模型生成编剧 Harness 的动态执行剧本。"""

        from openai import OpenAI

        client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        response = client.chat.completions.create(
            model=self.model,
            temperature=self.temperature,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        return response.choices[0].message.content or ""

