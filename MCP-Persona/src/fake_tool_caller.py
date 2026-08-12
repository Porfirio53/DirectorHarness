"""
Fake Tool Caller - 直接调用 fake tool 的接口

提供类似 main.py 中 get_tool_response 的接口，通过 tool_name 参数
直接调用 fake tool 并生成模拟结果。

Author: Claude Code
"""

import asyncio
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional, Tuple, Callable


class FakeToolError(Exception):
    """Fake tool 相关错误的基类"""

    pass


class ToolNotFoundError(FakeToolError):
    """找不到指定的工具"""

    pass




class ToolExecutionError(FakeToolError):
    """工具执行失败"""

    pass


def parse_tool_name(tool_name: str) -> Tuple[str, str]:
    """
    解析工具名称为 servername 和 toolname

    Args:
        tool_name: 工具名称，格式为 "servername:toolname"

    Returns:
        (servername, toolname) 元组

    Raises:
        ValueError: 工具名称格式错误
    """
    if ":" not in tool_name:
        raise ValueError("tool_name 格式错误，应为 'servername:toolname'")

    servername, toolname = tool_name.split(":", 1)
    if not servername or not toolname:
        raise ValueError("servername 和 toolname 都不能为空")

    return servername, toolname


def get_safe_tool_name(tool_name: str) -> str:
    """
    将工具名称转换为安全的文件名

    Args:
        tool_name: 工具名称，格式为 "servername:toolname"

    Returns:
        安全的文件名，将特殊字符替换为下划线
    """
    return tool_name.replace(':', '_').replace('/', '_')


def load_tools_description(mcp_name: str) -> Dict[str, Any]:
    """
    加载工具描述文件

    Args:
        mcp_name: MCP 服务器名称

    Returns:
        工具描述字典

    Raises:
        FileNotFoundError: 找不到工具描述文件
        json.JSONDecodeError: 工具描述文件格式错误
    """
    mcp_config_path = Path("mcp_configs") / mcp_name
    tools_desc_path = mcp_config_path / "tools_description.json"

    if not tools_desc_path.exists():
        raise FileNotFoundError(f"找不到工具描述文件: {tools_desc_path}")

    with open(tools_desc_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def load_python_code(mcp_name: str, tool_name: str) -> str:
    """
    从 pycode 目录加载 Python 代码

    Args:
        mcp_name: MCP 服务器名称
        tool_name: 工具名称

    Returns:
        Python 代码字符串

    Raises:
        FileNotFoundError: 找不到 Python 代码文件
        IOError: 文件读取失败
    """
    safe_tool_name = get_safe_tool_name(tool_name)
    pycode_path = Path("mcp_configs") / mcp_name / "pycode" / f"{safe_tool_name}.py"

    if not pycode_path.exists():
        raise FileNotFoundError(f"找不到 Python 代码文件: {pycode_path}")

    with open(pycode_path, 'r', encoding='utf-8') as f:
        return f.read()


def create_python_simulator(python_code: str, pycode_file_path: Path) -> Tuple[Callable, Dict[str, Any]]:
    """
    创建 Python 代码模拟器

    Args:
        python_code: Python 代码字符串
        pycode_file_path: Python 文件路径

    Returns:
        (编译好的 analyze_response_patterns 函数, 执行环境字典)

    Raises:
        RuntimeError: 代码编译失败或找不到目标函数
    """
    # 导入必要的模块
    from pathlib import Path as PathModule
    import time
    import uuid
    from datetime import datetime as datetime_module

    # 尝试导入 dateutil
    try:
        from dateutil.parser import parse as parse_date
        dateutil_available = True
    except ImportError:
        def parse_date(date_string):
            """简单的日期解析函数"""
            from datetime import datetime
            formats = ['%Y-%m-%dT%H:%M:%S%z', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d']
            for fmt in formats:
                try:
                    return datetime.strptime(date_string, fmt)
                except ValueError:
                    continue
            return datetime.now()
        dateutil_available = False

    # 创建安全的执行环境
    exec_globals = {
        '__builtins__': {
            'None': None, 'True': True, 'False': False,
            'bool': bool, 'int': int, 'float': float, 'str': str,
            'list': list, 'dict': dict, 'tuple': tuple, 'set': set,
            'len': len, 'min': min, 'max': max, 'abs': abs, 'round': round,
            'sum': sum, 'any': any, 'all': all, 'sorted': sorted,
            'range': range, 'enumerate': enumerate, 'zip': zip,
            'isinstance': isinstance, 'type': type, 'hasattr': hasattr,
            'getattr': getattr, 'setattr': setattr,
            'open': open, '__import__': __import__,
            'Exception': Exception, 'ValueError': ValueError,
            'KeyError': KeyError, 'TypeError': TypeError,
            'AttributeError': AttributeError,
            'json': json
        },
        'json': json,
        'Path': PathModule,
        'time': time,
        'uuid': uuid,
        'datetime': datetime_module,
        'status': 0,
        '__file__': str(pycode_file_path.resolve())
    }

    if dateutil_available:
        exec_globals['parse_date'] = parse_date
    else:
        exec_globals['parse_date'] = parse_date

    # 执行代码
    exec(python_code, exec_globals, exec_globals)

    # 获取目标函数
    analysis_function = exec_globals.get('analyze_response_patterns')
    if not analysis_function:
        raise RuntimeError("Python代码中未找到analyze_response_patterns函数")

    return analysis_function, exec_globals




async def call_fake_tool(
    tool_name: str,
    call_params: Optional[Dict[str, Any]] = None,
    mcp_configs_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """
    调用指定的 fake tool 并返回模拟结果

    Args:
        tool_name: 工具名称，格式为 "servername:toolname"
        call_params: 工具调用参数
        mcp_configs_dir: MCP配置目录路径（保留兼容性）

    Returns:
        包含工具执行结果的字典

    Raises:
        ValueError: 工具名称格式错误或找不到对应工具
        FileNotFoundError: 找不到 Python 代码文件
        RuntimeError: Python 代码编译或执行失败
    """
    logger = logging.getLogger("fake_tool_caller")

    try:
        # 1. 解析工具名称
        servername, toolname = parse_tool_name(tool_name)
        logger.info(f"调用 fake tool: {tool_name}")

        # 2. 加载工具描述
        tools_description = load_tools_description(servername)
        if tool_name not in tools_description:
            raise ValueError(f"在工具描述中找不到: {tool_name}")
        tool_info = tools_description[tool_name]
        logger.info(f"✅ 加载工具描述: {tool_name}")

        # 3. 加载 Python 代码
        python_code = load_python_code(servername, tool_name)
        safe_tool_name = get_safe_tool_name(tool_name)
        pycode_path = Path("mcp_configs") / servername / "pycode" / f"{safe_tool_name}.py"
        logger.info(f"✅ 加载 Python 代码: {pycode_path}")

        # 4. 创建执行器
        analysis_function, exec_globals = create_python_simulator(python_code, pycode_path)
        logger.info(f"✅ Python 代码编译成功")

        # 5. 执行函数
        logger.info(f"🚀 执行 fake tool 调用")
        result = analysis_function(call_params or {})

        # 6. 检查执行状态并记录日志
        if exec_globals.get('status') == 1:
            # 记录成功调用日志
            log_entry = {
                "tool_name": tool_name,
                "timestamp": datetime.now().isoformat(),
                "input_parameters": call_params or {},
                "output": result,
                "status": 1
            }

            log_file_path = "log.json"
            try:
                if os.path.exists(log_file_path):
                    with open(log_file_path, 'r', encoding='utf-8') as f:
                        logs = json.load(f)
                    if not isinstance(logs, list):
                        logs = []
                else:
                    logs = []

                logs.append(log_entry)

                with open(log_file_path, 'w', encoding='utf-8') as f:
                    json.dump(logs, f, ensure_ascii=False, indent=2)

                logger.info(f"💾 记录成功调用: {tool_name}")
            except Exception as e:
                logger.warning(f"写入日志失败: {e}")

        # 7. 返回结果
        return {
            "success": exec_globals.get('status', 0) == 1,
            "result": result,
            "error": result.get("error") if isinstance(result, dict) else None,
            "tool_name": tool_name,
            "parameters": call_params or {},
            "timestamp": datetime.now().isoformat(),
        }

    except (ValueError, FileNotFoundError, RuntimeError) as e:
        logger.error(f"❌ Fake tool 调用失败: {tool_name} - {e}")
        raise
    except Exception as e:
        logger.error(f"❌ Fake tool 执行异常: {tool_name} - {e}")
        raise RuntimeError(f"Fake tool 执行异常: {tool_name} - {e}")


# 示例使用函数
async def main_example():
    """
    示例用法
    """
    # 设置日志
    logging.basicConfig(level=logging.INFO)

    try:
        # 直接调用，无需 auto_build 参数
        result = await call_fake_tool(
            "xiaohongshu:search_feeds",
            {"data": {"keyword": "测试"}}
        )
        print("调用成功:", result)

    except FileNotFoundError as e:
        print(f"文件未找到: {e}")
    except RuntimeError as e:
        print(f"执行失败: {e}")
    except Exception as e:
        print(f"其他错误: {e}")


if __name__ == "__main__":
    # 运行示例
    asyncio.run(main_example())
