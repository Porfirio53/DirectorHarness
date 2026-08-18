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

        # #region debug-point A:request-config
        exec("try:\n import json as _j, urllib.request as _u, time as _t\n _p='.dbg/empty-judge-output.env'; _c=open(_p, encoding='utf-8').read(); _url=next((x.split('=',1)[1] for x in _c.splitlines() if x.startswith('DEBUG_SERVER_URL=')), 'http://127.0.0.1:7777/event'); _sid=next((x.split('=',1)[1] for x in _c.splitlines() if x.startswith('DEBUG_SESSION_ID=')), 'empty-judge-output'); _u.urlopen(_u.Request(_url, data=_j.dumps({'sessionId': _sid, 'runId': 'pre-fix', 'hypothesisId': 'A', 'location': 'writer_harness/llm_clients.py:37', 'msg': '[DEBUG] writer evaluator request configuration', 'data': {'model': self.model, 'has_base_url': bool(self.base_url), 'has_api_key': bool(self.api_key), 'system_prompt_length': len(system_prompt), 'user_prompt_length': len(user_prompt)}, 'ts': int(_t.time() * 1000)}).encode(), headers={'Content-Type':'application/json'}), timeout=1).read()\nexcept: pass")
        # #endregion
        client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        response = client.chat.completions.create(
            model=self.model,
            temperature=self.temperature,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            extra_body={"thinking": {"type": "disabled"}}
        )
        content = response.choices[0].message.content or ""
        # #region debug-point A:response-shape
        exec("try:\n import json as _j, urllib.request as _u, time as _t\n _p='.dbg/empty-judge-output.env'; _c=open(_p, encoding='utf-8').read(); _url=next((x.split('=',1)[1] for x in _c.splitlines() if x.startswith('DEBUG_SERVER_URL=')), 'http://127.0.0.1:7777/event'); _sid=next((x.split('=',1)[1] for x in _c.splitlines() if x.startswith('DEBUG_SESSION_ID=')), 'empty-judge-output'); _choice=response.choices[0]; _reasoning=getattr(_choice.message, 'reasoning_content', '') or ''; _u.urlopen(_u.Request(_url, data=_j.dumps({'sessionId': _sid, 'runId': 'pre-fix', 'hypothesisId': 'A', 'location': 'writer_harness/llm_clients.py:50', 'msg': '[DEBUG] writer evaluator response shape', 'data': {'content_length': len(content), 'finish_reason': getattr(_choice, 'finish_reason', None), 'reasoning_content_length': len(_reasoning), 'reasoning_starts_json': _reasoning.lstrip().startswith('{'), 'response_model': getattr(response, 'model', None)}, 'ts': int(_t.time() * 1000)}).encode(), headers={'Content-Type':'application/json'}), timeout=1).read()\nexcept: pass")
        # #endregion
        return content

