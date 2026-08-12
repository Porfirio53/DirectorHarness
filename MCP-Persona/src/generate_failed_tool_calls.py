#!/usr/bin/env python3
"""
自动生成失败的工具调用记录
根据成功调用记录，使用LLM生成会导致失败的输入参数，然后真实调用工具获取失败记录
"""

import asyncio
import json
import logging
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional, Set

from fake_tool.llm_client import LLMClient
from mcp_modules.server_manager import MultiServerManager

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("generate_failed_tool_calls")

# LLM配置（与Fake Tool一致）
LLM_API_BASE_URL = "http://123.129.219.111:3000/v1"
LLM_API_KEY = "<YOUR_API_KEY>"
LLM_MODEL = "gpt-4o"

# MCP配置目录
MCP_CONFIGS_DIR = Path("./MCP-Persona/mcp_configs/all-jsons")

# 运行时记录：每个工具已成功生成失败调用的输入历史（不持久化）
GENERATED_FAILURE_HISTORY: Dict[str, List[Dict[str, Any]]] = defaultdict(list)


def load_mcp_config(mcp_name: str) -> List[Dict[str, Any]]:
    """加载MCP配置文件"""
    config_path = MCP_CONFIGS_DIR / f"{mcp_name}.json"
    
    if not config_path.exists():
        # 尝试查找其他可能的配置文件（兼容旧格式）
        config_dir = MCP_CONFIGS_DIR.parent / mcp_name
        if config_dir.exists():
            json_files = list(config_dir.glob("*.json"))
            if json_files:
                config_path = json_files[0]
                logger.info(f"使用配置文件: {config_path}")
            else:
                raise FileNotFoundError(f"找不到MCP配置文件: {config_path}")
        else:
            raise FileNotFoundError(f"找不到MCP配置文件: {config_path}")
    
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    
    servers = []
    
    # for name, conf in cfg.get("mcpServers", {}).items():
    #     if conf.get("transport") == "sse":
    #         servers.append({
    #             "name": name,
    #             "url": conf.get("url"),
    #             "transport": conf.get("transport", "sse"),
    #         })
    #     else:
    #         servers.append({
    #             "name": name,
    #             "command": [conf.get("command")] + conf.get("args", []),
    #             "env": conf.get("env"),
    #             "cwd": conf.get("cwd"),
    #             "transport": conf.get("transport", "stdio"),
    #             "port": conf.get("port", None),
    #             "endpoint": conf.get("endpoint", "/mcp"),
    #             "url": conf.get("url", None)
    #         })
    
    # return servers
    for name, conf in cfg.get("mcpServers", {}).items():
        # 检查是否为 URL-based 连接（有 url 但没有 command）
        has_url = conf.get("url") is not None
        has_command = conf.get("command") is not None
        transport = conf.get("transport")
        # 检查 type 字段（某些配置使用 type 而不是 transport）
        config_type = conf.get("type")
        
        # 如果存在 type 字段，优先使用 type 来确定传输类型
        # streamable-http 本质上也是 HTTP 连接，只是支持流式响应
        if config_type == "streamable-http":
            transport = "http"
        elif config_type and not transport:
            # 如果只有 type 没有 transport，使用 type 作为 transport
            transport = config_type
        
        if has_url and not has_command:
            # URL-based 连接（如 Zapier, streamable-http 等）
            # 如果前面已经设置了 transport（如 streamable-http -> http），使用它；否则默认使用 sse
            final_transport = transport or "sse"
            servers.append({
                "name": name,
                "url": conf.get("url"),
                "transport": final_transport,
                "headers": conf.get("headers", {}),
            })
        elif transport == "sse":
            # 显式指定为 SSE 的配置
            servers.append({
                "name": name,
                "url": conf.get("url"),
                "transport": conf.get("transport", "sse"),
            })
        else:
            # 处理 command：如果 command 包含空格且没有 args，则拆分
            # 这样可以正确处理类似 "uvx server@latest" 这样的命令格式
            command_str = conf.get("command")
            args_list = conf.get("args", [])
            
            if command_str and isinstance(command_str, str):
                # 如果 command 包含空格且没有 args，则按空格拆分命令和参数
                # 例如: "uvx server@latest" -> ["uvx", "server@latest"]
                # 如果已有 args，则保持原样，因为空格可能是命令的一部分
                if " " in command_str and not args_list:
                    command_parts = command_str.split()
                    command = command_parts
                else:
                    # 原有逻辑：单个命令 + args
                    command = [command_str] + (args_list if args_list else [])
            else:
                # command 不存在或不是字符串，使用 args（如果存在）
                command = args_list if args_list else []
            
            servers.append({
                "name": name,
                "command": command,
                "env": conf.get("env"),
                "cwd": conf.get("cwd"),
                "transport": conf.get("transport", "stdio"),
                "port": conf.get("port", None),
                "endpoint": conf.get("endpoint", "/mcp"),
            })
    return servers



async def connect_mcp(mcp_name: str) -> tuple[MultiServerManager, Dict[str, Any]]:
    """连接MCP服务器并发现工具"""
    logger.info(f"📂 加载MCP配置: {mcp_name}")
    server_configs = load_mcp_config(mcp_name)
    
    if not server_configs:
        raise ValueError(f"配置文件中没有找到服务器配置")
    
    logger.info(f"🔌 连接 {len(server_configs)} 个MCP服务器...")
    # print(server_configs)
    manager = MultiServerManager(server_configs)
    
    try:
        all_tools = await manager.connect_all_servers()
        
        if all_tools is None:
            all_tools = {}
        
        if not all_tools:
            raise RuntimeError("未发现任何工具")
        
        logger.info(f"✅ 成功发现 {len(all_tools)} 个工具")
        return manager, all_tools
    except Exception as e:
        logger.error(f"❌ 连接失败: {e}")
        raise




def load_success_records(record_file: Path) -> List[Dict[str, Any]]:
    """加载成功调用记录（数组格式）"""
    try:
        with open(record_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            # 确保返回数组格式
            if isinstance(data, list):
                return data
            elif isinstance(data, dict):
                # 如果是单个对象，转换为数组
                return [data]
            else:
                logger.error(f"未知的记录格式: {record_file}")
                return []
    except Exception as e:
        logger.error(f"加载成功记录失败: {record_file}: {e}")
        raise


def find_tool_record_files(mcp_dir: Path) -> Dict[str, Path]:
    """查找所有工具的成功调用记录文件（排除failed_开头的文件）"""
    tool_records = {}
    
    for json_file in mcp_dir.glob("*.json"):
        file_name = json_file.stem  # 不含扩展名
        
        # 排除特定文件
        if file_name in {"tools_description", "usage_summary"}:
            continue
        
        # 排除已失败的记录（新格式：failed_{tool_name}.json）
        if file_name.startswith("failed_"):
            continue
        
        tool_records[file_name] = json_file
    
    return tool_records


def extract_parameters_from_success_records(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """从成功记录数组中提取所有调用参数"""
    parameters_list = []
    
    for record in records:
        # 优先从 tool_call.parameters_used 提取
        if "tool_call" in record and "parameters_used" in record["tool_call"]:
            params = record["tool_call"]["parameters_used"]
            # 如果参数中有 data 字段，提取 data 的内容
            if isinstance(params, dict) and "data" in params:
                parameters_list.append(params["data"])
            else:
                parameters_list.append(params)
        # 其次从 manual_input.parameters 提取
        elif "manual_input" in record and "parameters" in record["manual_input"]:
            params = record["manual_input"]["parameters"]
            # 如果参数中有 data 字段，提取 data 的内容
            if isinstance(params, dict) and "data" in params:
                parameters_list.append(params["data"])
            else:
                parameters_list.append(params)
    
    return parameters_list


def extract_tool_info_from_records(records: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """从成功记录数组中提取工具信息（描述、schema等）"""
    if not records:
        return None
    
    # 从第一条记录中提取工具信息
    first_record = records[0]
    
    return {
        "tool_name": first_record.get("tool_name", ""),
        "original_name": first_record.get("original_name", ""),
        "server": first_record.get("server", ""),
        "description": first_record.get("tool_description", ""),
        "input_schema": first_record.get("input_schema", {})
    }


async def generate_failed_input_with_llm(
    llm_client: LLMClient,
    tool_name: str,
    tool_description: str,
    input_schema: Dict[str, Any],
    success_inputs: List[Dict[str, Any]],
    existing_failure_types: Set[str],
    historical_failures: List[Dict[str, Any]],
    attempt_number: int
) -> Optional[Dict[str, Any]]:
    """使用LLM生成会导致失败的输入参数"""
    
    success_inputs_str = json.dumps(success_inputs[:3], indent=2, ensure_ascii=False)  # 只展示前3个成功输入
    
    # 汇总历史失败类型（运行时记录）
    historical_failure_types = [
        record.get("failure_type")
        for record in historical_failures
        if record.get("failure_type")
    ]
    if historical_failure_types:
        existing_failure_types.update(historical_failure_types)
    
    existing_types_str = ", ".join(list(existing_failure_types)[:5]) if existing_failure_types else "无"
    
    # 最近的历史失败示例（最多3条）
    recent_failures = historical_failures[-3:] if historical_failures else []
    recent_failures_summary = json.dumps(
        [
            {
                "failure_type": record.get("failure_type", "unknown"),
                "input": record.get("input", {}),
                "reason": record.get("reason", "")
            }
            for record in recent_failures
        ],
        indent=2,
        ensure_ascii=False
    ) if recent_failures else "无"
    
    # 打印调试信息
    print(f"\n🔍 生成失败输入 - 尝试 #{attempt_number}")
    print(f"   工具: {tool_name}")
    print(f"   已有失败类型: {existing_types_str}")
    if recent_failures:
        print(f"   最近失败示例数量: {len(recent_failures)}")
    
    prompt = f"""You are an API testing expert. Your task is to generate input parameters that will cause a tool call to FAIL.

Tool name: {tool_name}
Tool description: {tool_description}
Input schema: {json.dumps(input_schema, indent=2, ensure_ascii=False)}

Examples of successful inputs (for reference):
{success_inputs_str}

Existing failure types already generated (avoid duplicating these):
{existing_types_str}

Previously successful failing inputs for this tool (avoid repeating the same failure types):
{recent_failures_summary}

Your task:
1. Generate input parameters that will cause this tool to fail when called
2. The failure should be due to invalid input, not external factors
3. Try different failure types:
   - Type mismatch (e.g., string instead of number, wrong enum value)
   - Missing required fields
   - Invalid format (e.g., invalid email, invalid phone number, invalid date)
   - Invalid value range (e.g., negative number when positive required)
   - Invalid combination (e.g., message_type is "image" but message_content is text)
   - Invalid identifier (e.g., non-existent ID, malformed UUID)
   - Empty or null values for required fields (if appropriate)
   - Invalid nested structure

4. For attempt #{attempt_number}, try to generate a failure type that is DIFFERENT from the existing ones

5. CRITICAL RULES FOR ATTEMPT #{attempt_number}:
   - If attempt_number > 1: You MUST NOT return empty input {{}}. You MUST generate specific invalid parameter values.
   - If attempt_number == 1: You may return empty input {{}} ONLY if the tool might accept it as valid (to test that case).
   - For attempt_number > 1: You MUST provide at least one parameter with an invalid value (wrong type, invalid format, out of range, etc.).
   - DO NOT return empty object {{}} for attempt_number > 1. Always provide invalid parameter values.

Return ONLY a JSON object with the input parameters that will cause failure. Do not include any explanation, just the JSON object.

Example format:
{{
  "param1": "invalid_value",
  "param2": 123
}}

Return JSON only, with no extra text."""

    try:
        response = await llm_client.call_llm(
            prompt,
            temperature=0.7,  # 稍高温度以增加多样性
            max_tokens=1000
        )
        
        # 打印LLM的原始响应
        print(f"🤖 LLM原始响应: {response}")
        
        # 解析JSON响应
        json_pattern = r"\{[\s\S]*\}"
        match = re.search(json_pattern, response, re.DOTALL)
        if match:
            try:
                failed_input = json.loads(match.group(0))
                print(f"✅ LLM生成的失败输入: {json.dumps(failed_input, indent=2, ensure_ascii=False)}")
                return failed_input
            except json.JSONDecodeError as e:
                logger.warning(f"❌ 解析LLM响应失败: {e}")
                logger.warning(f"尝试解析的内容: {match.group(0)[:500]}")
                return None
        else:
            logger.warning(f"❌ 未找到JSON对象，LLM响应: {response[:500]}")
            return None
            
    except Exception as e:
        logger.error(f"LLM生成失败输入失败: {e}")
        return None


async def is_success_response_with_llm(
    llm_client: LLMClient,
    tool_name: str,
    tool_description: str,
    input_schema: Dict[str, Any],
    input_parameters: Dict[str, Any],
    tool_result: Any
) -> tuple[bool, str, Optional[str]]:
    """使用LLM判断工具调用是否成功，返回 (is_success, reason, failure_type)"""
    
    # 将结果转换为字符串
    if isinstance(tool_result, dict):
        result_str = json.dumps(tool_result, indent=2, ensure_ascii=False)
    elif isinstance(tool_result, (list, str)):
        result_str = json.dumps(tool_result, indent=2, ensure_ascii=False) if isinstance(tool_result, list) else tool_result
    else:
        result_str = str(tool_result)
    
    # 如果结果太长，截断但保留关键信息
    if len(result_str) > 2000:
        result_str = result_str[:2000] + "..."
    
    prompt = f"""You are an API tool analysis expert. Analyze a tool call and classify it as SUCCESS or ERROR based on whether the response is meaningful and fulfills the tool's intended purpose.

Tool name: {tool_name}
Tool description: {tool_description}
Tool input schema: {json.dumps(input_schema, indent=2, ensure_ascii=False)}

Call record:
Input: {json.dumps(input_parameters, indent=2, ensure_ascii=False)}
Complete response: {result_str}

CRITICAL Classification Rules:
1. A call is SUCCESS if:
   - The response contains meaningful data that fulfills the tool's purpose according to the tool description
   - The response structure indicates successful completion
   - Even if there are warnings or partial data, if the core purpose is achieved, it's SUCCESS
   - The response provides useful information according to the tool description
   - The response helps accomplish what the tool is designed to do

2. A call is ERROR if:
   - The response indicates failure (error codes, error messages)
   - The response is null/empty when data should be returned according to tool description
   - The response structure indicates failure
   - Authentication/authorization failures
   - Invalid input parameters that cause the tool to fail
   - Any response that does NOT fulfill the tool's intended purpose as described
   - Connection errors, timeouts, or other technical failures

3. IMPORTANT: Do NOT rely solely on fields like "success", "error", "isError" - analyze the ACTUAL RESPONSE CONTENT:
   - Check if the response contains the expected data according to tool description
   - Check if the response structure matches successful responses
   - Check if error messages indicate actual failures
   - Consider the tool's purpose: does this response help achieve it?

Return JSON in the following format:
{{
  "is_success": true/false,
  "reason": "brief explanation",
  "failure_type": "type of failure (if error)" or null
}}

Return JSON only, with no extra explanations."""

    try:
        response = await llm_client.call_llm(
            prompt,
            temperature=0.1,
            max_tokens=500
        )
        
        # 解析LLM响应
        json_pattern = r"\{[\s\S]*\}"
        match = re.search(json_pattern, response, re.DOTALL)
        if match:
            try:
                result = json.loads(match.group(0))
                is_success = result.get("is_success", False)
                reason = result.get("reason", "")
                failure_type = result.get("failure_type", None)
                return is_success, reason, failure_type
            except json.JSONDecodeError:
                logger.warning(f"解析LLM响应失败: {response[:200]}")
        
        # 如果解析失败，回退判断
        logger.warning("LLM判断失败，使用回退判断")
        is_success = _is_success_fallback(tool_result)
        return is_success, "回退判断", None
        
    except Exception as e:
        logger.warning(f"LLM判断失败: {e}，使用回退判断")
        return _is_success_fallback(tool_result), f"LLM判断异常: {e}", None


def _is_success_fallback(tool_result: Any) -> bool:
    """回退判断（硬编码）"""
    if tool_result is None:
        return False
    
    if isinstance(tool_result, dict):
        # 检查常见错误字段
        if "error" in tool_result:
            return False
        if "isError" in tool_result and tool_result["isError"]:
            return False
        if "success" in tool_result and not tool_result["success"]:
            return False
        # 如果包含result字段且不为空，可能是成功
        if "result" in tool_result and tool_result["result"] is not None:
            return True
        # 如果字典不为空，可能是成功
        if tool_result:
            return True
    
    if isinstance(tool_result, (list, str)):
        return bool(tool_result)
    
    return True


def convert_result_to_dict(result: Any) -> Any:
    """将工具结果转换为可序列化的字典格式"""
    if result is None:
        return None
    
    if isinstance(result, (dict, list, str, int, float, bool)):
        return result
    
    # 尝试转换为字符串
    try:
        result_str = str(result)
        # 尝试将字符串解析为 JSON（如果结果是 JSON 字符串）
        try:
            return json.loads(result_str)
        except (json.JSONDecodeError, TypeError):
            return result_str
    except Exception:
        pass
    
    # 如果所有方法都失败，返回对象的字符串表示
    return {"_raw_type": type(result).__name__, "_raw_str": str(result)}


def save_failed_record(
    mcp_dir: Path,
    tool_name: str,
    original_name: str,
    server_name: str,
    tool_description: str,
    input_schema: Dict[str, Any],
    failed_input: Dict[str, Any],
    tool_result: Any,
    timestamp: float
):
    """保存失败调用记录（追加模式，格式与成功记录一致）"""
    # 使用 original_name 作为文件名，与成功记录的文件命名保持一致
    # 文件名格式：failed_{tool_name}.json（将failed放在开头）
    file_name = original_name if original_name else tool_name.split(":")[-1]
    failed_file = mcp_dir / f"failed_{file_name}.json"
    
    # 转换结果为可序列化格式
    result_dict = convert_result_to_dict(tool_result)
    
    # 构建调用记录（格式与成功记录一致）
    call_record = {
        "tool_name": tool_name,
        "original_name": original_name,
        "server": server_name,
        "tool_description": tool_description,
        "input_schema": input_schema,
        "manual_input": {
            "parameters": {
                "data": failed_input  # 参数包装在 data 字段中
            }
        },
        "tool_call": {
            "parameters_used": {
                "data": failed_input  # 参数包装在 data 字段中
            },
            "success": False,
            "error": None,
            "result": result_dict
        },
        "timestamp": timestamp
    }
    
    # 如果文件已存在，读取现有记录
    if failed_file.exists():
        try:
            with open(failed_file, "r", encoding="utf-8") as f:
                existing_records = json.load(f)
            if not isinstance(existing_records, list):
                existing_records = []
        except Exception as e:
            logger.warning(f"读取现有失败记录失败: {e}，创建新文件")
            existing_records = []
    else:
        existing_records = []
    
    # 追加新记录
    existing_records.append(call_record)
    
    # 保存
    with open(failed_file, "w", encoding="utf-8") as f:
        json.dump(existing_records, f, indent=2, ensure_ascii=False)
    
    logger.info(f"💾 已保存失败记录到: {failed_file} (总计 {len(existing_records)} 条)")


async def process_tool(
    mcp_dir: Path,
    mcp_name: str,
    tool_name: str,
    tool_record_file: Path,
    manager: MultiServerManager,
    all_tools: Dict[str, Any],
    llm_client: LLMClient,
    num_failures: int
):
    """处理单个工具，生成失败调用记录"""
    logger.info(f"\n{'='*60}")
    logger.info(f"处理工具: {tool_name}")
    logger.info(f"{'='*60}")
    
    # 加载成功记录
    success_records = load_success_records(tool_record_file)
    
    if not success_records:
        logger.warning(f"工具 {tool_name} 没有成功调用记录，跳过")
        return
    
    # 从成功记录中提取工具信息
    tool_info_from_records = extract_tool_info_from_records(success_records)
    if not tool_info_from_records:
        logger.warning(f"无法从成功记录中提取工具信息，跳过")
        return
    
    # 从成功记录中获取完整的工具名称（格式：server:tool_name）
    full_tool_name = tool_info_from_records.get("tool_name", "")
    original_name = tool_info_from_records.get("original_name", "")
    server_name = tool_info_from_records.get("server", "")
    
    # 如果成功记录中没有完整工具名，尝试构建：server:original_name
    if not full_tool_name and server_name and original_name:
        full_tool_name = f"{server_name}:{original_name}"
    # 如果还是没有，尝试使用 mcp_name:original_name
    elif not full_tool_name and original_name:
        full_tool_name = f"{mcp_name}:{original_name}"
    # 如果还是没有，尝试使用传入的 tool_name（可能是完整格式）
    elif not full_tool_name:
        full_tool_name = tool_name
    
    # 检查工具是否存在（优先使用完整格式）
    actual_tool_name = None
    # print("all_tools: ", all_tools)
    if full_tool_name in all_tools:
        actual_tool_name = full_tool_name
    elif tool_name in all_tools:
        actual_tool_name = tool_name
    elif original_name in all_tools:
        actual_tool_name = original_name
    else:
        logger.warning(f"工具 {full_tool_name} (或 {tool_name} 或 {original_name}) 不在已发现的工具列表中，跳过")
        logger.debug(f"可用的工具名称: {list(all_tools.keys())[:10]}...")
        return
    
    tool_info = all_tools[actual_tool_name]
    # 如果工具信息中没有 server，使用从记录中提取的
    if not tool_info.get("server") and server_name:
        tool_info["server"] = server_name
    
    description = tool_info_from_records.get("description", "")
    input_schema = tool_info_from_records.get("input_schema", {})
    
    # 提取成功调用的参数
    success_inputs = extract_parameters_from_success_records(success_records)
    
    if not success_inputs:
        logger.warning(f"工具 {tool_name} 没有成功调用参数，跳过")
        return
    
    logger.info(f"找到 {len(success_inputs)} 个成功调用记录")
    
    # 读取已存在的失败记录，了解已有的失败类型
    # 使用 original_name 构建文件名，格式：failed_{original_name}.json
    file_name_for_failed = original_name if original_name else tool_name.split(":")[-1]
    failed_file = mcp_dir / f"failed_{file_name_for_failed}.json"
    existing_failure_types: Set[str] = set()
    runtime_failure_history = GENERATED_FAILURE_HISTORY[actual_tool_name]
    
    if failed_file.exists():
        try:
            with open(failed_file, "r", encoding="utf-8") as f:
                existing_failed_records = json.load(f)
                if isinstance(existing_failed_records, list):
                    existing_count = len(existing_failed_records)
                else:
                    existing_count = existing_failed_records.get("total_calls", 0)
                logger.info(f"已存在 {existing_count} 条失败记录")
                
                # 如果已有足够的失败记录，跳过
                if existing_count >= num_failures:
                    logger.info(f"已有足够的失败记录（{existing_count} >= {num_failures}），跳过")
                    return
        except Exception as e:
            logger.warning(f"读取现有失败记录失败: {e}")
    
    # 生成失败调用
    generated_count = 0
    max_attempts = num_failures * 3  # 最多尝试3倍次数，以确保获得足够的失败记录
    
    for attempt in range(1, max_attempts + 1):
        if generated_count >= num_failures:
            break
        
        logger.info(f"\n尝试 #{attempt} (已生成 {generated_count}/{num_failures} 条失败记录)")
        
        # 第一次尝试必须探索空输入
        if attempt == 1:
            logger.info("第一次尝试：探索空输入 {}")
            failed_input = {}
        else:
            # 生成失败输入
            failed_input = await generate_failed_input_with_llm(
                llm_client,
                tool_name,
                description,
                input_schema,
                success_inputs,
                existing_failure_types,
                runtime_failure_history,
                attempt
            )
            
            if not failed_input:
                logger.warning("LLM未能生成失败输入，跳过此次尝试")
                continue
        
        logger.info(f"生成的失败输入: {json.dumps(failed_input, indent=2, ensure_ascii=False)}")
        
        # 调用工具（使用实际工具名称）
        try:
            logger.info(f"🚀 调用工具: {actual_tool_name}")
            tool_result = await manager.call_tool(actual_tool_name, failed_input, use_cache=False)
            logger.info("✅ 工具调用完成")
        except Exception as e:
            # 调用异常，视为失败
            error_msg = str(e)
            logger.info(f"❌ 工具调用异常: {error_msg}")
            tool_result = {
                "error": error_msg,
                "isError": True
            }
        
        # 判断是否真的失败（使用完整工具名称用于日志）
        is_success, reason, failure_type = await is_success_response_with_llm(
            llm_client,
            full_tool_name or actual_tool_name,
            description,
            input_schema,
            failed_input,
            tool_result
        )
        
        if is_success:
            logger.info(f"⚠️ 调用成功（不符合预期）: {reason}")
            continue
        
        # 确实是失败，保存记录
        logger.info(f"✅ 确认失败: {reason}")
        if failure_type:
            existing_failure_types.add(failure_type)
        
        # 生成时间戳（与成功记录格式一致）
        timestamp = asyncio.get_event_loop().time()
        
        # 记录运行时失败历史（仅内存）
        runtime_failure_history.append({
            "input": failed_input,
            "failure_type": failure_type or "unknown",
            "reason": reason,
            "timestamp": timestamp
        })
        # 只保留最近5条，避免提示过长
        if len(runtime_failure_history) > 5:
            del runtime_failure_history[:-5]
        
        # 保存失败记录（使用完整工具名称，与成功记录格式一致）
        save_failed_record(
            mcp_dir,
            full_tool_name or actual_tool_name,  # 使用完整格式的工具名称
            original_name,
            server_name,
            description,
            input_schema,
            failed_input,
            tool_result,
            timestamp
        )
        
        generated_count += 1
    
    logger.info(f"\n✅ 工具 {tool_name} 处理完成，共生成 {generated_count} 条失败记录")


async def main():
    """主函数"""
    import argparse
    
    parser = argparse.ArgumentParser(description="自动生成失败的工具调用记录")
    parser.add_argument(
        "mcp_dir",
        type=str,
        help="MCP工具调用记录目录（例如: mcp_configs/all-server-results/amap-mcp-server）"
    )
    parser.add_argument(
        "--num-failures",
        type=int,
        default=3,
        help="每个工具需要生成的失败记录数量（默认: 3）"
    )
    parser.add_argument(
        "--mcp-name",
        type=str,
        default=None,
        help="MCP名称（如果不提供，将从目录名推断）"
    )
    
    args = parser.parse_args()
    
    mcp_dir = Path(args.mcp_dir)
    if not mcp_dir.exists():
        logger.error(f"目录不存在: {mcp_dir}")
        sys.exit(1)
    
    # 推断MCP名称
    if args.mcp_name:
        mcp_name = args.mcp_name
    else:
        # 从目录名推断（例如：amap-mcp-server）
        mcp_name = mcp_dir.name
        logger.info(f"从目录名推断MCP名称: {mcp_name}")
    
    num_failures = args.num_failures
    
    logger.info(f"开始处理MCP: {mcp_name}")
    logger.info(f"目录: {mcp_dir}")
    logger.info(f"每个工具生成失败记录数: {num_failures}")
    
    # 初始化LLM客户端
    llm_client = LLMClient(LLM_API_BASE_URL, LLM_API_KEY, LLM_MODEL)
    
    # 连接MCP服务器（用于验证工具是否存在）
    try:
        manager, all_tools = await connect_mcp(mcp_name)
    except Exception as e:
        logger.error(f"连接MCP服务器失败: {e}")
        sys.exit(1)
    
    # 查找所有工具记录文件
    tool_records = find_tool_record_files(mcp_dir)
    
    if not tool_records:
        logger.warning(f"未找到任何工具记录文件: {mcp_dir}")
        sys.exit(0)
    
    logger.info(f"找到 {len(tool_records)} 个工具记录文件")
    
    # 处理每个工具
    for tool_name, record_file in tool_records.items():
        try:
            await process_tool(
                mcp_dir,
                mcp_name,
                tool_name,
                record_file,
                manager,
                all_tools,
                llm_client,
                num_failures
            )
        except Exception as e:
            logger.error(f"处理工具 {tool_name} 失败: {e}")
            import traceback
            logger.error(traceback.format_exc())
            continue
    
    # 关闭连接
    await manager.close_all_connections()
    
    logger.info("\n✅ 所有工具处理完成")


if __name__ == "__main__":
    asyncio.run(main())

