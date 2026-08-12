"""命令行入口：将用户参数转换为三类演员/编剧 Harness baseline 的一次执行。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .actor_harness import OpenHarnessCLIActorExecutor
from .llm_clients import OpenAICompatibleClient
from .models import ActorBackend, HarnessRequest, InteractionMode, LLMBackend
from .orchestrator import InteractionOrchestrator
from .prompts import detect_language
from .writer_harness import WriterHarness


def build_parser() -> argparse.ArgumentParser:
    """构建 writer_harness 命令行参数解析器。

    该解析器统一描述两种交互模式，以及真实的编剧/演员 Harness 配置入口。
    当前不再保留 actor 或 writer 的 mock 模式，以确保实验只比较
    actor_harness 单独执行与 actor_harness + writer_harness 两种路径。
    """

    parser = argparse.ArgumentParser(description="用户-编剧 Harness（可选）-演员 Harness 双模式交互框架")
    parser.add_argument("--mode", choices=[item.value for item in InteractionMode], default=InteractionMode.VANILLA.value)
    parser.add_argument("--query", default=None, help="用户任务指令")
    parser.add_argument("--query-file", default=None, help="从文件读取用户任务指令，适合超长输入")
    parser.add_argument("--actor-backend", choices=[item.value for item in ActorBackend], default=ActorBackend.OPENHARNESS.value)
    parser.add_argument("--writer-backend", choices=[item.value for item in LLMBackend], default=LLMBackend.OPENAI_COMPATIBLE.value)
    parser.add_argument("--writer-model", default="", help="编剧 Harness 模型名称，openai-compatible 模式下必填")
    parser.add_argument("--writer-base-url", default=None, help="OpenAI-compatible 编剧模型 API base_url")
    parser.add_argument("--writer-api-key", default=None, help="OpenAI-compatible 编剧模型 API key")
    parser.add_argument("--oh-bin", default="oh", help="OpenHarness CLI 可执行文件名或路径")
    parser.add_argument("--openharness-src", default=None, help="OpenHarness 源码 src 目录；用于源码安装或 PYTHONPATH 方式运行")
    parser.add_argument("--oh-real-run", action="store_true", help="默认使用 OpenHarness dry-run；开启后会真实调用 oh 执行")
    parser.add_argument("--actor-model", default=None, help="演员 Harness 使用 OpenHarness 时，覆盖其执行模型")
    parser.add_argument("--actor-base-url", default=None, help="演员 Harness 使用 OpenHarness 时，覆盖其 base_url")
    parser.add_argument("--actor-api-key", default=None, help="演员 Harness 使用 OpenHarness 时，覆盖其 API key")
    parser.add_argument("--actor-api-format", default=None, help="演员 Harness 使用 OpenHarness 时，覆盖其 API format，如 anthropic / openai / copilot")
    parser.add_argument("--actor-output-format", default=None, help="演员 Harness 使用 OpenHarness 时，覆盖其输出格式，如 text / json / stream-json")
    parser.add_argument("--save-final-prompt", default=None, help="保存最终交给演员 Harness 的 prompt")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出执行结果摘要")
    return parser


def build_writer_harness(args: argparse.Namespace) -> WriterHarness | None:
    """根据命令行参数构建编剧 Harness。

    参数说明：
    - args.mode: 仅当模式为 writer_harness 时，才需要真实启用编剧 Harness。
    - args.writer_model/writer_base_url/writer_api_key: 控制编剧侧真实模型接口。
    """

    if args.mode != InteractionMode.WRITER_HARNESS.value:
        return None
    if not args.writer_model:
        raise ValueError("--writer-backend openai-compatible 时必须提供 --writer-model")
    return WriterHarness(OpenAICompatibleClient(model=args.writer_model, base_url=args.writer_base_url, api_key=args.writer_api_key))


def build_actor_executor(args: argparse.Namespace):
    """根据命令行参数构建演员 Harness 执行器。

    当前仅支持真实 OpenHarness CLI 路径，不再保留 mock actor 分支。
    """

    return OpenHarnessCLIActorExecutor(
        oh_bin=args.oh_bin,
        dry_run=not args.oh_real_run,
        output_format=args.actor_output_format,
        openharness_src=args.openharness_src,
        model=args.actor_model,
        base_url=args.actor_base_url,
        api_key=args.actor_api_key,
        api_format=args.actor_api_format,
    )
def safe_print(text: str) -> None:
    """安全打印文本，避免 Windows 控制台编码问题影响输出。"""

    try:
        print(text)
    except UnicodeEncodeError:
        encoded = text.encode(sys.stdout.encoding or "utf-8", errors="replace")
        sys.stdout.buffer.write(encoded + b"\n")


def safe_json_print(payload: dict) -> None:
    """安全打印 JSON 结果，保证 CLI 模式下摘要可直接复制使用。"""

    safe_print(json.dumps(payload, ensure_ascii=False, indent=2))


def serialize_script_report(report) -> dict | None:
    """把结构化剧本对象标准化为普通字典。

    该函数允许上层同时接收 dataclass 对象和已经序列化好的 dict，
    便于 JSON 输出保持稳定结构。
    """

    if report is None:
        return None
    if hasattr(report, "to_dict"):
        return report.to_dict()
    if isinstance(report, dict):
        return report
    return None


def main() -> None:
    """运行 writer_harness 命令行主流程。

    主流程负责：解析参数、组装真实编剧/演员 Harness、执行 orchestrator，
    并根据用户选择输出 JSON 摘要或更适合人读的控制台文本。
    """

    parser = build_parser()
    args = parser.parse_args()
    if not args.query and not args.query_file:
        parser.error("--query 或 --query-file 至少提供一个")
    if args.query_file:
        args.query = Path(args.query_file).read_text(encoding="utf-8")
    if args.query is None:
        parser.error("无法读取用户任务指令")
    mode = InteractionMode(args.mode)
    writer_harness = build_writer_harness(args)
    actor_executor = build_actor_executor(args)
    orchestrator = InteractionOrchestrator(actor_executor=actor_executor, writer_harness=writer_harness)
    request = HarnessRequest(query=args.query, mode=mode)
    result = orchestrator.run(request)

    if args.save_final_prompt:
        Path(args.save_final_prompt).write_text(result.final_prompt, encoding="utf-8")

    if args.json:
        safe_json_print(
            {
                "ok": result.ok,
                "mode": result.mode,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "return_code": result.return_code,
                "script_report": serialize_script_report(result.script_report),
                "script_report_source": result.script_report_source,
                "script_report_object_origin": result.script_report_object_origin,
                "script_report_transport_path": result.script_report_transport_path,
                "actor_harness_report": result.actor_harness_report,
                "final_script_report": result.final_script_report,
                "online_completeness_judgment": result.online_completeness_judgment,
                "judge_completeness_evaluation": result.judge_completeness_evaluation,
                "final_prompt": result.final_prompt,
            }
        )
        return

    safe_print("=" * 80)
    safe_print("最终交给演员 Harness 的 Prompt")
    safe_print("=" * 80)
    safe_print(result.final_prompt)
    safe_print("\n" + "=" * 80)
    safe_print("演员 Harness 输出")
    safe_print("=" * 80)
    if result.stdout:
        safe_print(result.stdout)
    if result.stderr:
        safe_print("[stderr]")
        safe_print(result.stderr)


if __name__ == "__main__":
    main()
