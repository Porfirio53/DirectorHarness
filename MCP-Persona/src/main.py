import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional

from mcp_modules.server_manager import MultiServerManager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mcp_tool_caller")


def print_tools_one_per_line(all_tools: Dict[str, Any]) -> None:
    """将工具字典按“每个工具一行”输出，单行 JSON 便于查看或管道处理。"""
    for tool_key, tool_meta in (all_tools or {}).items():
        try:
            print(json.dumps({tool_key: tool_meta}, ensure_ascii=False))
        except Exception:
            # 兜底：无法 JSON 序列化时，直接输出字符串形式
            print(f"{tool_key}: {tool_meta}")


def save_tools_to_file(config_path: str | Path, all_tools: Dict[str, Any], output_root: str | Path = "mcp_configs/all-server-tools", overwrite: bool = False) -> Path:
    """
    将发现到的所有工具保存到以“原配置文件名”命名的 JSON 中。
    例如: config=/.../amap-mcp-server.json -> 保存到 /.../all-server-tools/amap-mcp-server.json
    """
    cfg_path = Path(config_path)
    output_dir = Path(output_root)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / cfg_path.name
    if output_file.exists() and not overwrite:
        logger.info(f"ℹ️ Tools file already exists, skip writing: {output_file}")
        return output_file
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(all_tools or {}, f, ensure_ascii=False, indent=2, default=str)
    return output_file


def save_result_to_timestamped_file(
    config_path: str | Path,
    result_data: Any,
    output_root: str | Path = "mcp_configs/all-server-results"
) -> Path:
    """
    将本次调用结果保存到:
      /.../all-server-results/{config_name_without_ext}/{YYYYMMDD_HHMMSS}.json
    """
    cfg_path = Path(config_path)
    # 约定使用配置文件名（不含扩展名）作为子目录名
    server_folder = cfg_path.stem
    output_dir = Path(output_root) / server_folder
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = output_dir / f"{ts}.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(result_data, f, ensure_ascii=False, indent=2, default=str)
    return output_file


def extract_tool_name(tool_name: str) -> str:
    """从完整的工具名中提取纯工具名，例如：从 'amap-mcp-server:maps_weather' 提取 'maps_weather'"""
    if ":" in tool_name:
        return tool_name.split(":", 1)[1]
    return tool_name


def extract_server_name(tool_name: str) -> str:
    """从完整的工具名中提取服务器名，例如：从 'amap-mcp-server:maps_weather' 提取 'amap-mcp-server'"""
    if ":" in tool_name:
        return tool_name.split(":", 1)[0]
    return "unknown_server"


def init_tool_history(tool_name: str, server_name: str) -> Dict[str, Any]:
    """初始化工具调用历史记录结构"""
    return {
        "tool_name": tool_name,
        "server": server_name,
        "call_history": [],
        "total_calls": 0,
        "last_updated": None
    }


def save_result_to_tool_file(
    config_path: str | Path,
    tool_name: str,
    call_params: Dict[str, Any],
    result_data: Any,
    output_root: str | Path = "mcp_configs/all-server-results",
    tools_info: Optional[Dict[str, Any]] = None
) -> Path:
    """
    将调用结果保存到工具专属的历史记录文件中:
      /.../all-server-results/{config_name_without_ext}/{tool_name}.json

    支持两种格式：
    - Amap格式（字典）: {tool_name, server, call_history: [...], total_calls, last_updated}
    - Lark格式（数组）: [{tool_name, manual_input, tool_call, timestamp}, ...]

    如果文件已存在，保持原有格式；如果不存在，默认创建Amap格式。
    tools_info: 可选，用于Lark格式时提供工具元数据（从save_tools_to_file生成的文件）
    """
    cfg_path = Path(config_path)
    # 约定使用配置文件名（不含扩展名）作为子目录名
    server_folder = cfg_path.stem
    output_dir = Path(output_root) / server_folder
    output_dir.mkdir(parents=True, exist_ok=True)

    # 提取纯工具名和服务器名
    pure_tool_name = extract_tool_name(tool_name)
    server_name = extract_server_name(tool_name)

    # 工具专属历史文件路径
    output_file = output_dir / f"{pure_tool_name}.json"

    # 生成时间戳和易读时间
    now = datetime.now()
    timestamp = now.strftime("%Y%m%d_%H%M%S")
    readable_time = now.strftime("%Y-%m-%d %H:%M:%S")

    # 读取现有数据或初始化新结构
    file_format = None  # 'amap' 或 'lark'
    existing_data = None

    if output_file.exists():
        try:
            with open(output_file, "r", encoding="utf-8") as f:
                existing_data = json.load(f)

            # 检测文件格式
            if isinstance(existing_data, dict) and "call_history" in existing_data:
                file_format = "amap"
            elif isinstance(existing_data, list):
                file_format = "lark"
            else:
                # 未知格式，重新初始化为amap格式
                logger.info(f"检测到未知格式，将使用amap格式: {output_file}")
                file_format = "amap"
                existing_data = None
        except (json.JSONDecodeError, FileNotFoundError):
            # 文件损坏，重新初始化
            existing_data = None
    else:
        # 文件不存在，默认使用amap格式
        file_format = "amap"

    # 根据格式处理数据
    if file_format == "amap":
        # Amap格式（字典）
        if existing_data is None:
            tool_history = init_tool_history(pure_tool_name, server_name)
        else:
            tool_history = existing_data

        # 创建新的调用记录
        new_call_record = {
            "timestamp": timestamp,
            "call_time": readable_time,
            "call_params": call_params,
            "result": result_data
        }

        # 追加新的调用记录
        tool_history["call_history"].append(new_call_record)
        tool_history["total_calls"] += 1
        tool_history["last_updated"] = readable_time

        # 写回文件
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(tool_history, f, ensure_ascii=False, indent=2, default=str)

    elif file_format == "lark":
        # Lark格式（数组）
        if existing_data is None:
            existing_data = []

        # 获取工具元数据
        tool_meta = {}
        if tools_info and tool_name in tools_info:
            tool_info = tools_info[tool_name]
            tool_meta = {
                "tool_name": tool_name,
                "original_name": tool_info.get("original_name", pure_tool_name),
                "server": server_name,
                "tool_description": tool_info.get("description", ""),
                "input_schema": tool_info.get("input_schema", {})
            }
        else:
            # 如果没有工具元数据，使用基本信息
            tool_meta = {
                "tool_name": tool_name,
                "original_name": pure_tool_name,
                "server": server_name,
                "tool_description": "",
                "input_schema": {}
            }

        # 检查result是否有错误
        is_error = False
        error_msg = None
        if isinstance(result_data, dict):
            if result_data.get("isError"):
                is_error = True
                error_msg = "Tool call failed"

        # 创建新的调用记录（Lark格式）
        timestamp_float = now.timestamp() * 1000000  # 转换为微秒
        new_call_record = {
            **tool_meta,
            "manual_input": {
                "parameters": {
                    "data": call_params
                }
            },
            "tool_call": {
                "parameters_used": {
                    "data": call_params
                },
                "success": not is_error,
                "error": error_msg,
                "result": result_data
            },
            "timestamp": timestamp_float
        }

        # 追加到数组
        existing_data.append(new_call_record)

        # 写回文件
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(existing_data, f, ensure_ascii=False, indent=2, default=str)

    return output_file


def load_server_configs(config_path: Path):
    """从 MCP config 文件加载并转换 server 配置"""
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    servers = []
    
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


# async def get_tool_response(
#     config_path: str | Path,
#     call_params: Optional[Dict[str, Any]] = None,
#     tool_name: Optional[str] = None,
# ) -> Any:
#     """连接 MCP server 并调用工具，返回结果"""
#     config_path = Path(config_path)
#     if not config_path.exists():
#         raise FileNotFoundError(f"Config file not found: {config_path}")

#     server_configs = load_server_configs(config_path)
#     if not server_configs:
#         raise ValueError("No servers found in config file.")

#     manager = MultiServerManager(server_configs)
#     try:
#         logger.info("🔌 Connecting and discovering tools...")
#         all_tools = await manager.connect_all_servers()
#         if not all_tools:
#             raise RuntimeError("No tools discovered.")

#         # 如果未指定工具，则默认取第一个
#         if tool_name is None:
#             tool_name = next(iter(all_tools.keys()))
#         if tool_name not in all_tools:
#             print(all_tools)
#             raise ValueError(f"Tool '{tool_name}' not found.")

#         logger.info(f"🚀 Calling tool: {tool_name}")
#         result = await manager.call_tool(tool_name, call_params or {}, use_cache=False)
#         logger.info("✅ Tool call SUCCESS.")
#         return result

#     finally:
#         await manager.close_all_connections()

# async def ():
    

async def initialize_servers(config_path: str | Path) -> Dict[str, Any]:
    """
    连接所有 MCP servers 并发现工具，返回所有工具的字典。
    """
    config_path = Path(config_path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    server_configs = load_server_configs(config_path)
    if not server_configs:
        raise ValueError("No servers found in config file.")
    print(server_configs)
    manager = MultiServerManager(server_configs)
    try:
        logger.info("🔌 Connecting and discovering tools...")
        all_tools = await manager.connect_all_servers()
        if not all_tools:
            raise RuntimeError("No tools discovered.")
        logger.info(f"✅ Discovered {len(all_tools)} tools.")
        # print(all_tools)
        return manager, all_tools
    except Exception as e:
        logger.error(f"Error in discover_all_tools: {e}")
        raise
    # finally:
    #     await manager.close_all_connections()


# async def get_tool_response(
#     config_path: str | Path,
#     call_params: Optional[Dict[str, Any]] = None,
#     tool_name: Optional[str] = None,
# ) -> Any:
async def get_tool_response(
    manager: MultiServerManager,
    call_params: Optional[Dict[str, Any]] = None,
    tool_name: Optional[str] = None,
) -> Any:
    """
    调用 MCP server 中的指定工具，并返回执行结果。
    """
    # all_tools = await discover_all_tools(config_path)

    # if tool_name is None:
    #     tool_name = next(iter(all_tools.keys()))

    # if tool_name not in all_tools:
    #     logger.error(f"❌ Tool '{tool_name}' not found. Available tools: {list(all_tools.keys())}")
    #     raise ValueError(f"Tool '{tool_name}' not found.")

    # 重新加载 manager 用于实际调用（因为 discover_all_tools 里关闭了连接）
    # server_configs = load_server_configs(config_path)
    # manager = MultiServerManager(server_configs)

    try:
        # await manager.connect_all_servers()
        logger.info(f"🚀 Calling tool: {tool_name}")
        print(call_params)
        result = await manager.call_tool(tool_name, call_params or {}, use_cache=False)
        logger.info("✅ Tool call SUCCESS.")
        return result
    finally:
        await manager.close_all_connections()


# ===== 示例调用 =====
if __name__ == "__main__":
    async def run_example():

        config_path = "mcp_configs/all-jsons/slack.json"
        manager, all_tools = await initialize_servers(config_path)
        print_tools_one_per_line(all_tools)
        save_path = save_tools_to_file(config_path, all_tools)
        logger.info(f"📝 Tools saved to: {save_path}")
        # result = await get_tool_response(
        #     manager,
        #     tool_name="default-server:check_login_status",
        #     call_params={}
        # )
        # 定义工具名和调用参数
        tool_name = "slack:send_message"
        call_params ={"channel": "DHBT0CV8R55", "text": "Hello, world!"}
  
        result = await get_tool_response(
            manager,
            tool_name=tool_name,
            call_params=call_params
        )

        if hasattr(result, "model_dump"):
            result_data = result.model_dump()
        elif hasattr(result, "dict"):
            result_data = result.dict()
        else:
            result_data = result

        # 保存结果到工具专属的历史记录文件
        result_path = save_result_to_tool_file(config_path, tool_name, call_params, result_data, tools_info=all_tools)
        logger.info(f"🗂️ Result saved to: {result_path}")

        print(json.dumps(result_data, indent=2, ensure_ascii=False))

    asyncio.run(run_example())
