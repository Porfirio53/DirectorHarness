"""封装演员 Harness 执行后端。

当前仅保留基于 OpenHarness CLI 的真实执行路径，确保实验对比聚焦于
“是否启用 writer_harness”而不是“是否使用 mock actor”。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile

from .models import ExecutionResult, InteractionMode, WriterHarnessReport


def extract_report_metadata_from_stdout(stdout: str) -> tuple[dict | None, str | None, str | None, str | None]:
    """从演员 Harness 的标准输出中提取结构化执行剧本。

    参数说明：
    - stdout: 演员 Harness 的原始标准输出文本，可能是纯文本，也可能夹带 JSON。

    返回值依次为：
    - script_report: 解析出的结构化剧本对象；
    - source: 剧本来源标记，描述结果来自何处；
    - origin: 剧本对象的产生主体；
    - transport: 剧本在链路中的传递路径。
    """

    text = stdout.strip()
    if not text:
        return None, None, None, None
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        return None, None, None, None
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None, None, None, None
    if not isinstance(payload, dict):
        return None, None, None, None
    source = payload.get("script_report_source")
    origin = payload.get("script_report_object_origin")
    transport = payload.get("script_report_transport_path")
    if isinstance(payload.get("script_report"), dict):
        return payload["script_report"], source or "actor_harness_output", origin or "actor_harness", transport or "actor_harness_output"
    if {"task_profile", "difficulty_profile", "execution_plan"}.issubset(payload.keys()):
        return payload, source or "actor_harness_output", origin or "actor_harness", transport or "actor_harness_output"
    return None, None, None, None


class ActorHarnessExecutor:
    """演员 Harness 执行器抽象。

    目前仅有真实 OpenHarness CLI 实现，但仍保留抽象层，方便后续扩展
    其他真实执行后端，而不影响 orchestrator 与在线执行脚本。
    """

    def execute(self, prompt: str, mode: InteractionMode, script_report: WriterHarnessReport | None = None) -> ExecutionResult:
        """执行演员 Harness。

        参数说明：
        - prompt: 最终要交给演员 Harness 的文本输入。
        - mode: 当前交互模式，用于标记是直连、固定脚本还是编剧生成剧本。
        - script_report: 编剧 Harness 已产出的结构化执行剧本；若提供，
          可随结果一并透传，方便在线执行脚本或上层 orchestrator 继续消费。
        """

        raise NotImplementedError


class OpenHarnessCLIActorExecutor(ActorHarnessExecutor):
    """通过 `oh -p` 接入 OpenHarness，把 OpenHarness 作为真实演员 Harness 执行系统。"""

    def __init__(
        self,
        oh_bin: str = "oh",
        dry_run: bool = True,
        output_format: str | None = None,
        openharness_src: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        api_format: str | None = None,
    ):
        """初始化基于 OpenHarness CLI 的演员执行器。

        参数说明：
        - oh_bin: OpenHarness CLI 可执行文件名或绝对路径。
        - dry_run: 是否只做 dry-run，不真正进入工具执行。
        - output_format: 演员 Harness 输出格式，如 text / json / stream-json。
        - openharness_src: OpenHarness 源码 src 路径；当本地未正式安装时，
          用于通过 PYTHONPATH 注入源码。
        - model/base_url/api_key/api_format: 透传给 OpenHarness 的模型配置。
        """

        self.oh_bin = oh_bin
        self.dry_run = dry_run
        self.output_format = output_format
        self.openharness_src = openharness_src
        self.model = model
        self.base_url = base_url
        self.api_key = api_key
        self.api_format = api_format

    def execute(self, prompt: str, mode: InteractionMode, script_report: WriterHarnessReport | None = None) -> ExecutionResult:
        """调用 OpenHarness CLI 执行真实演员 Harness 任务。"""

        if shutil.which(self.oh_bin) is None:
            return ExecutionResult(
                ok=False,
                mode=mode.value,
                final_prompt=prompt,
                stdout="",
                stderr=f"未找到 OpenHarness CLI：{self.oh_bin}。请先安装 OpenHarness 并确认 CLI 可用。",
                return_code=127,
                script_report=script_report,
                script_report_source=("actor_harness_passthrough_script_report" if script_report is not None else None),
                script_report_object_origin=("writer_harness" if script_report is not None else None),
                script_report_transport_path=("writer_harness_to_actor_harness_passthrough" if script_report is not None else None),
            )
        prompt_file_path: str | None = None
        command = [self.oh_bin]
        if self.dry_run:
            command.append("--dry-run")
        if self.model:
            command.extend(["--model", self.model])
        if self.base_url:
            command.extend(["--base-url", self.base_url])
        if self.api_key:
            command.extend(["--api-key", self.api_key])
        if self.api_format:
            command.extend(["--api-format", self.api_format])
        if len(prompt) > 4000:
            temp_file = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8")
            temp_file.write(prompt)
            temp_file.close()
            prompt_file_path = temp_file.name
            command.extend(["-p", f"@{prompt_file_path}"])
        else:
            command.extend(["-p", prompt])
        if self.output_format:
            command.extend(["--output-format", self.output_format])
        env = os.environ.copy()
        if self.openharness_src:
            existing_pythonpath = env.get("PYTHONPATH")
            env["PYTHONPATH"] = self.openharness_src if not existing_pythonpath else f"{self.openharness_src}{os.pathsep}{existing_pythonpath}"
        try:
            completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
            actor_report, report_source, report_origin, report_transport = extract_report_metadata_from_stdout(completed.stdout)
            return ExecutionResult(
                ok=completed.returncode == 0,
                mode=mode.value,
                final_prompt=prompt,
                stdout=completed.stdout,
                stderr=completed.stderr,
                return_code=completed.returncode,
                script_report=script_report,
                script_report_source=report_source,
                script_report_object_origin=report_origin,
                script_report_transport_path=report_transport,
                final_script_report=actor_report,
            )
        finally:
            if prompt_file_path:
                try:
                    os.unlink(prompt_file_path)
                except OSError:
                    pass
