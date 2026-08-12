#!/usr/bin/env python3
"""
批量构建 Fake Tool 脚本
给定 context 文件、工具调用目录和工具描述文件，为每个工具构建 Fake Tool
"""

import asyncio
import json
import logging
import pickle
import sys
from pathlib import Path
from typing import Dict, Any, List, Optional, Set

from fake_tool.fake_tool_builder import FakeToolBuilder
from fake_tool.llm_tool_simulator import LLMToolSimulator
from fake_tool.tool_context_builder import ToolContextBuilder
from fake_tool.llm_client import LLMClient

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("build_fake_tools_batch")

# Fake Tool LLM配置
FAKE_TOOL_API_BASE_URL = "http://152.53.53.64:3000/v1"
FAKE_TOOL_API_KEY = "<YOUR_API_KEY>"
FAKE_TOOL_MODEL = "gpt-5"

# 需要排除的文件名模式
EXCLUDED_FILES = {
    "tools_description.json",
    "usage_summary.json",
    "feishu.json",  # 配置文件
    "feishu_mcp_tools_zh.json",
    "feishu_mcp_tools_en.json",
    "context.json",  # Context 文件
    "validation_*.json",  # 验证结果文件
}

EXCLUDED_PREFIXES = {
    "fake_",  # Fake Tool 使用记录
    "fake_tool_cache_",  # Fake Tool 缓存
}


def get_safe_tool_name(tool_name: str) -> str:
    """将工具名称转换为安全的文件名"""
    return tool_name.replace(":", "_").replace("/", "_").replace("\\", "_")


def get_fake_tool_cache_path(output_dir: Path, tool_name: str) -> Path:
    """获取fake tool缓存文件路径"""
    safe_tool_name = get_safe_tool_name(tool_name)
    return output_dir / f"fake_tool_cache_{safe_tool_name}.pkl"


def save_fake_tool_cache(output_dir: Path, tool_name: str, simulator: LLMToolSimulator):
    """保存fake tool缓存"""
    cache_path = get_fake_tool_cache_path(output_dir, tool_name)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    
    try:
        # 保存ToolContext（不包含LLM客户端引用）
        cache_data = {
            "tool_context": simulator.context,
            "tool_name": tool_name
        }
        
        with open(cache_path, "wb") as f:
            pickle.dump(cache_data, f)
        
        logger.info(f"💾 已保存 Fake Tool 缓存: {cache_path}")
    except Exception as e:
        logger.warning(f"保存缓存失败: {e}")


def load_fake_tool_cache(output_dir: Path, tool_name: str) -> Optional[LLMToolSimulator]:
    """加载fake tool缓存"""
    cache_path = get_fake_tool_cache_path(output_dir, tool_name)
    
    if not cache_path.exists():
        return None
    
    try:
        with open(cache_path, "rb") as f:
            cache_data = pickle.load(f)
        
        # 重建simulator（需要LLM客户端）
        llm_client = LLMClient(
            api_base_url=FAKE_TOOL_API_BASE_URL,
            api_key=FAKE_TOOL_API_KEY,
            model=FAKE_TOOL_MODEL
        )
        
        simulator = LLMToolSimulator(
            tool_context=cache_data["tool_context"],
            llm_client=llm_client
        )
        
        logger.info(f"✅ 从缓存加载 Fake Tool: {tool_name}")
        return simulator
    except Exception as e:
        logger.warning(f"加载缓存失败: {e}")
        return None


def extract_tool_name_from_record(record_file: Path) -> Optional[str]:
    """从记录文件中提取工具名称"""
    try:
        with open(record_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        # 支持数组或单个对象格式
        if isinstance(data, list):
            if len(data) > 0 and isinstance(data[0], dict):
                tool_name = data[0].get("tool_name")
                if tool_name:
                    return tool_name
        elif isinstance(data, dict):
            tool_name = data.get("tool_name")
            if tool_name:
                return tool_name
        
        logger.warning(f"无法从记录文件中提取工具名称: {record_file}")
        return None
    except Exception as e:
        logger.error(f"读取记录文件失败 {record_file}: {e}")
        return None


def find_tool_record_files(tool_call_dir: Path) -> Dict[str, Path]:
    """查找所有工具记录文件，返回 {tool_name: record_file_path} 的字典"""
    tool_records = {}
    
    # 遍历目录下所有 JSON 文件
    for json_file in tool_call_dir.glob("*.json"):
        file_name = json_file.name
        
        # 排除特定文件
        if file_name in EXCLUDED_FILES:
            continue
        
        # 排除特定前缀的文件
        if any(file_name.startswith(prefix) for prefix in EXCLUDED_PREFIXES):
            continue
        
        # 排除匹配模式的文件（如 validation_*.json）
        if file_name.startswith("validation_"):
            continue
        
        # 尝试从文件中提取工具名称
        tool_name = extract_tool_name_from_record(json_file)
        if tool_name:
            tool_records[tool_name] = json_file
            logger.debug(f"找到工具记录: {tool_name} -> {json_file.name}")
        else:
            logger.warning(f"跳过文件（无法提取工具名称）: {json_file.name}")
    
    return tool_records


def load_tools_description(tools_description_file: Path) -> Dict[str, Any]:
    """加载工具描述文件"""
    if not tools_description_file.exists():
        raise FileNotFoundError(f"工具描述文件不存在: {tools_description_file}")
    
    try:
        with open(tools_description_file, "r", encoding="utf-8") as f:
            tools_description = json.load(f)
        
        if not isinstance(tools_description, dict):
            raise ValueError(f"工具描述文件格式错误: 应为字典类型")
        
        logger.info(f"✅ 成功加载工具描述文件: {tools_description_file}")
        logger.info(f"   包含 {len(tools_description)} 个工具描述")
        return tools_description
    except Exception as e:
        logger.error(f"加载工具描述文件失败: {e}")
        raise


def load_usage_records(tool_call_dir: Path, tool_name: str) -> List[Dict[str, Any]]:
    """加载工具使用记录"""
    safe_tool_name = get_safe_tool_name(tool_name)
    record_file = tool_call_dir / f"{safe_tool_name}.json"
    
    if not record_file.exists():
        return []
    
    try:
        with open(record_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        if isinstance(data, list):
            return data
        elif isinstance(data, dict):
            return [data]
        return []
    except Exception as e:
        logger.warning(f"加载使用记录失败 {record_file}: {e}")
        return []


def get_tool_description_from_dict(tools_description: Dict[str, Any], tool_name: str) -> Optional[Dict[str, Any]]:
    """从工具描述字典中获取指定工具的描述"""
    if tool_name in tools_description:
        return tools_description[tool_name]
    
    # 尝试查找（可能工具名格式不同）
    for key, value in tools_description.items():
        if key == tool_name or key.endswith(tool_name) or tool_name.endswith(key):
            logger.info(f"   找到匹配的工具描述: {key} (查找: {tool_name})")
            return value
    
    logger.warning(f"工具 {tool_name} 不在工具描述文件中")
    return None


async def generate_and_save_pycode(
    builder: FakeToolBuilder,
    tool_name: str,
    tool_call_dir: Path,
    tool_description: Any,  # ToolContext
    usage_records: List[Dict[str, Any]],
    context_file: Path,
    structure_info: Optional[Dict[str, Any]] = None
):
    """
    生成并保存 pycode 文件
    
    Args:
        builder: FakeToolBuilder 实例
        tool_name: 工具名称
        tool_call_dir: 工具调用目录（输出目录）
        tool_description: 工具描述（ToolContext）
        usage_records: 使用记录
        context_file: Context 文件路径
        structure_info: Context 结构信息（从 generate_dynamic_context_handler 获取）
    """
    # pycode 生成到 tool_call_dir/pycode/
    pycode_dir = tool_call_dir / "pycode"
    pycode_dir.mkdir(parents=True, exist_ok=True)
    
    # 获取工具描述字典
    tool_desc_dict = {
        "tool_name": tool_description.tool_name,
        "description": tool_description.tool_description,
        "input_schema": tool_description.input_schema
    }
    
    # 提取成功和错误示例
    success_examples = tool_description.success_examples
    error_examples = tool_description.error_examples
    
    # 创建 ToolContextBuilder 并设置 LLM 客户端
    context_builder = ToolContextBuilder()
    context_builder.set_llm_client(builder.llm_client)
    
    # 从 context_file 路径提取信息
    context_file_name = context_file.name
    context_dir = context_file.parent
    
    # 生成 Python 代码
    try:
        # 生成 dynamic context 的 pycode
        if not structure_info:
            structure_info = {}
        
        python_code = await context_builder.generate_python_code_for_dynamic_context(
            tool_name=tool_name,
            tool_description=tool_desc_dict,
            mcp_name="",  # 不再需要 mcp_name
            mcp_dir=tool_call_dir,  # 使用 tool_call_dir
            context_group_dir="",  # 不再需要 context_group_dir
            context_file_name=context_file_name,
            structure_info=structure_info,
            usage_records=usage_records,
            success_examples=success_examples,
            error_examples=error_examples
        )
        
        # 验证生成的代码
        if not python_code or not python_code.strip():
            raise ValueError("生成的代码为空")
        
        # 检查是否包含必要的函数定义
        if "def analyze_response_patterns" not in python_code:
            raise ValueError("生成的代码缺少 analyze_response_patterns 函数定义")
        
        # 保存到文件
        safe_tool_name = get_safe_tool_name(tool_name)
        pycode_file = pycode_dir / f"{safe_tool_name}.py"
        
        with open(pycode_file, "w", encoding="utf-8") as f:
            f.write(python_code)
        
        logger.info(f"   💾 已保存 pycode: {pycode_file}")
    except Exception as e:
        logger.error(f"   ❌ 生成 pycode 失败: {e}")
        # 如果文件已存在但生成失败，删除空文件
        safe_tool_name = get_safe_tool_name(tool_name)
        pycode_file = pycode_dir / f"{safe_tool_name}.py"
        if pycode_file.exists() and pycode_file.stat().st_size == 0:
            pycode_file.unlink()
            logger.info(f"   🗑️  已删除空文件: {pycode_file}")
        raise


async def generate_dynamic_context_handler(
    builder: FakeToolBuilder,
    context_file: Path,
    tool_call_dir: Path,
    tool_names: List[str]
) -> Dict[str, Any]:
    """
    生成 dynamic context handler（支持嵌套字典结构）
    
    Args:
        builder: FakeToolBuilder 实例
        context_file: Context 文件路径
        tool_call_dir: 工具调用目录（输出目录）
        tool_names: 该组的所有工具名称列表
    
    Returns:
        structure_info: Context 结构分析结果
    """
    handler_file = tool_call_dir / "dynamic_context_handler.py"
    
    # 如果 handler 已存在，跳过生成（同一组共享一个 handler）
    if handler_file.exists():
        logger.info(f"   ✅ Dynamic context handler 已存在: {handler_file}")
        # 仍然需要返回 structure_info，从 context 文件分析
        if context_file.exists():
            try:
                with open(context_file, 'r', encoding='utf-8') as f:
                    raw_data = json.load(f)
                # 支持字典格式：{context_id: context_dict, ...}
                # 支持列表格式：[{context_dict}, ...]（向后兼容）
                if isinstance(raw_data, dict):
                    # 新格式：字典，key 是 context_id，value 是 context 字典
                    # 取前 2 个 context，保持字典格式
                    context_keys = list(raw_data.keys())[:2]
                    context_data_dict = {k: raw_data[k] for k in context_keys}
                    logger.info(f"   ℹ️  Context 是字典格式，使用前 {len(context_keys)} 个 context（共 {len(raw_data)} 个）")
                    # 分析结构时使用第一个 context 的字典
                    context_data_for_analysis = raw_data[context_keys[0]] if context_keys else {}
                elif isinstance(raw_data, list):
                    # 旧格式：列表，向后兼容
                    if len(raw_data) > 0 and isinstance(raw_data[0], dict):
                        # 取前 2 个 user，包装成新列表
                        context_data_list = raw_data[:2]
                        logger.info(f"   ℹ️  Context 是列表，使用前 {len(context_data_list)} 个元素（共 {len(raw_data)} 个）")
                        # 分析结构时使用第一个 user 的字典
                        context_data_for_analysis = context_data_list[0]
                    else:
                        logger.warning(f"   ⚠️  Context 列表为空或第一个元素不是字典，跳过")
                        return {}
                else:
                    logger.warning(f"   ⚠️  Context 格式不支持: {type(raw_data).__name__}")
                    return {}
                
                context_builder = ToolContextBuilder()
                structure_info = context_builder.analyze_nested_context_structure(
                    context_data_for_analysis, tool_names, []
                )
                return structure_info
            except Exception as e:
                logger.warning(f"   ⚠️  读取 context 文件失败: {e}")
        return {}
    
    # 读取 context 文件
    if not context_file.exists():
        logger.warning(f"   ⚠️  未找到 context 文件: {context_file}，跳过生成 handler")
        return {}
    
    try:
        with open(context_file, 'r', encoding='utf-8') as f:
            raw_data = json.load(f)
        
        # 支持字典格式：{context_id: context_dict, ...}
        # 支持列表格式：[{context_dict}, ...]（向后兼容）
        if isinstance(raw_data, dict):
            # 新格式：字典，key 是 context_id，value 是 context 字典
            # 取前 2 个 context，保持字典格式
            context_keys = list(raw_data.keys())[:2]
            context_data_dict = {k: raw_data[k] for k in context_keys}
            logger.info(f"   ℹ️  Context 是字典格式，使用前 {len(context_keys)} 个 context（共 {len(raw_data)} 个）")
            # 分析结构时使用第一个 context 的字典
            context_data_for_analysis = raw_data[context_keys[0]] if context_keys else {}
        elif isinstance(raw_data, list):
            # 旧格式：列表，向后兼容
            if len(raw_data) > 0 and isinstance(raw_data[0], dict):
                # 取前 2 个 user，包装成新列表
                context_data_list = raw_data[:2]
                logger.info(f"   ℹ️  Context 是列表，使用前 {len(context_data_list)} 个元素（共 {len(raw_data)} 个）")
                # 分析结构时使用第一个 user 的字典
                context_data_for_analysis = context_data_list[0]
            else:
                logger.warning(f"   ⚠️  Context 列表为空或第一个元素不是字典，跳过生成 handler")
                return {}
        else:
            logger.warning(f"   ⚠️  Context 格式不支持: {type(raw_data).__name__}，跳过生成 handler")
            return {}
    except Exception as e:
        logger.warning(f"   ⚠️  读取 context 文件失败: {e}，跳过生成 handler")
        return {}
    
    # 加载所有工具的使用记录
    all_usage_records = []
    for tool_name in tool_names:
        records = load_usage_records(tool_call_dir, tool_name)
        all_usage_records.extend(records)
    
    # 创建 ToolContextBuilder 并分析结构
    context_builder = ToolContextBuilder()
    context_builder.set_llm_client(builder.llm_client)
    
    # 分析嵌套结构（使用第一个 user 的字典来分析结构）
    structure_info = context_builder.analyze_nested_context_structure(
        context_data_for_analysis, tool_names, all_usage_records
    )
    
    try:
        context_file_name = context_file.name
        # 确定传递给 handler 的 context_data 格式
        if isinstance(raw_data, dict):
            # 新格式：传递字典（包含前 2 个 context）
            context_data_for_handler = context_data_dict
        else:
            # 旧格式：传递列表（包含前 2 个 user）
            context_data_for_handler = context_data_list
        
        handler_code = await context_builder.generate_dynamic_context_handler_code(
            context_data=context_data_for_handler,  # 传递字典或列表
            structure_info=structure_info,
            tool_names=tool_names,
            usage_records=all_usage_records,
            context_file_name=context_file_name
        )
        
        if not handler_code or not handler_code.strip():
            raise ValueError("生成的 handler 代码为空")
        
        # 保存 handler 到 tool_call_dir
        tool_call_dir.mkdir(parents=True, exist_ok=True)
        with open(handler_file, "w", encoding="utf-8") as f:
            f.write(handler_code)
        
        logger.info(f"   💾 已生成 dynamic context handler: {handler_file}")
        return structure_info
    except Exception as e:
        logger.error(f"   ❌ 生成 dynamic context handler 失败: {e}")
        raise


async def build_fake_tools(
    context_file: Path,
    tool_call_dir: Path,
    tools_description_file: Path,
    use_cache: bool = True,
    use_llm_analysis: bool = True,
    force_rebuild: bool = False
) -> Dict[str, LLMToolSimulator]:
    """
    为指定目录下的所有工具构建 Fake Tool
    
    Args:
        context_file: Context 文件路径
        tool_call_dir: 工具调用记录保存的文件夹（输出目录）
        tools_description_file: 工具描述文件路径
        use_cache: 是否使用缓存（如果存在）
        use_llm_analysis: 是否使用 LLM 分析错误模式
        force_rebuild: 是否强制重新构建（忽略缓存）
    
    Returns:
        构建的 Fake Tool 字典 {tool_name: simulator}
    """
    context_file = Path(context_file)
    tool_call_dir = Path(tool_call_dir)
    tools_description_file = Path(tools_description_file)
    
    # 验证输入文件/目录
    if not context_file.exists():
        raise FileNotFoundError(f"Context 文件不存在: {context_file}")
    
    if not tool_call_dir.exists():
        raise FileNotFoundError(f"工具调用目录不存在: {tool_call_dir}")
    
    if not tools_description_file.exists():
        raise FileNotFoundError(f"工具描述文件不存在: {tools_description_file}")
    
    logger.info("="*60)
    logger.info("📂 输入参数")
    logger.info("="*60)
    logger.info(f"Context 文件: {context_file}")
    logger.info(f"工具调用目录: {tool_call_dir}")
    logger.info(f"工具描述文件: {tools_description_file}")
    logger.info(f"输出目录: {tool_call_dir}")
    
    # 1. 加载工具描述文件
    logger.info("\n" + "="*60)
    logger.info("📖 加载工具描述文件...")
    logger.info("="*60)
    
    tools_description = load_tools_description(tools_description_file)
    
    # 2. 查找所有工具记录文件
    logger.info("\n" + "="*60)
    logger.info("🔍 查找工具记录文件...")
    logger.info("="*60)
    
    tool_records = find_tool_record_files(tool_call_dir)
    
    if not tool_records:
        logger.warning("❌ 未找到任何工具记录文件")
        return {}
    
    logger.info(f"✅ 找到 {len(tool_records)} 个工具记录文件:")
    for tool_name, record_file in tool_records.items():
        logger.info(f"   - {tool_name} ({record_file.name})")
    
    # 3. 创建 FakeToolBuilder
    logger.info("\n" + "="*60)
    logger.info("🔨 初始化 Fake Tool 构建器...")
    logger.info("="*60)
    
    # 不再需要 mcp_configs_dir，直接使用工具描述文件
    builder = FakeToolBuilder(
        mcp_configs_dir=str(tool_call_dir),  # 这个参数在 build_from_records 中不再使用
        api_base_url=FAKE_TOOL_API_BASE_URL,
        api_key=FAKE_TOOL_API_KEY,
        model=FAKE_TOOL_MODEL
    )
    
    # 4. 生成 dynamic context handler（只生成一次）
    logger.info("\n" + "="*60)
    logger.info("🔧 生成 Dynamic Context Handler...")
    logger.info("="*60)
    
    tool_names = list(tool_records.keys())
    structure_info = {}
    
    try:
        structure_info = await generate_dynamic_context_handler(
            builder=builder,
            context_file=context_file,
            tool_call_dir=tool_call_dir,
            tool_names=tool_names
        )
        logger.info(f"   ✅ Dynamic Context Handler 准备完成")
    except Exception as e:
        logger.warning(f"   ⚠️  生成 dynamic context handler 失败: {e}")
        import traceback
        logger.debug(traceback.format_exc())
    
    # 5. 为每个工具构建 Fake Tool
    logger.info("\n" + "="*60)
    logger.info("🔧 开始构建 Fake Tools...")
    logger.info("="*60)
    
    fake_tools = {}
    success_count = 0
    cache_count = 0
    error_count = 0
    
    for i, (tool_name, record_file) in enumerate(tool_records.items(), 1):
        logger.info(f"\n[{i}/{len(tool_records)}] 处理工具: {tool_name}")
        logger.info(f"   记录文件: {record_file.name}")
        
        try:
            # 检查缓存
            if use_cache and not force_rebuild:
                cached_simulator = load_fake_tool_cache(tool_call_dir, tool_name)
                if cached_simulator:
                    fake_tools[tool_name] = cached_simulator
                    cache_count += 1
                    logger.info(f"   ✅ 使用缓存")
                    
                    # 即使使用缓存，也生成 pycode（如果不存在）
                    safe_tool_name = get_safe_tool_name(tool_name)
                    pycode_file = tool_call_dir / "pycode" / f"{safe_tool_name}.py"
                    if not pycode_file.exists():
                        try:
                            # 需要从工具描述文件加载工具描述
                            tool_desc_dict = get_tool_description_from_dict(tools_description, tool_name)
                            if tool_desc_dict:
                                # 创建临时 ToolContext（用于生成 pycode）
                                usage_records = load_usage_records(tool_call_dir, tool_name)
                                context_builder = ToolContextBuilder()
                                context_builder.set_llm_client(builder.llm_client)
                                tool_context = await context_builder.build_context_async(
                                    tool_desc_dict,
                                    usage_records,
                                    use_llm_analysis=use_llm_analysis
                                )
                                
                                await generate_and_save_pycode(
                                    builder=builder,
                                    tool_name=tool_name,
                                    tool_call_dir=tool_call_dir,
                                    tool_description=tool_context,
                                    usage_records=usage_records,
                                    context_file=context_file,
                                    structure_info=structure_info
                                )
                        except Exception as e:
                            logger.warning(f"   ⚠️  生成 pycode 失败: {e}")
                    continue
            
            # 获取工具描述
            tool_desc_dict = get_tool_description_from_dict(tools_description, tool_name)
            if not tool_desc_dict:
                logger.warning(f"   ⚠️  跳过工具（未找到工具描述）: {tool_name}")
                error_count += 1
                continue
            
            # 构建新的 Fake Tool
            logger.info(f"   🔨 构建 Fake Tool...")
            
            # 加载使用记录
            usage_records = load_usage_records(tool_call_dir, tool_name)
            
            # 创建 ToolContextBuilder 并构建上下文
            context_builder = ToolContextBuilder()
            context_builder.set_llm_client(builder.llm_client)
            
            tool_context = await context_builder.build_context_async(
                tool_desc_dict,
                usage_records,
                use_llm_analysis=use_llm_analysis
            )
            
            # 创建 LLM Simulator
            simulator = LLMToolSimulator(
                tool_context=tool_context,
                llm_client=builder.llm_client
            )
            
            fake_tools[tool_name] = simulator
            success_count += 1
            
            # 保存缓存
            save_fake_tool_cache(tool_call_dir, tool_name, simulator)
            logger.info(f"   ✅ 构建成功并已缓存")
            
            # 生成并保存 pycode
            try:
                await generate_and_save_pycode(
                    builder=builder,
                    tool_name=tool_name,
                    tool_call_dir=tool_call_dir,
                    tool_description=tool_context,
                    usage_records=usage_records,
                    context_file=context_file,
                    structure_info=structure_info
                )
                logger.info(f"   💾 已生成并保存 pycode")
            except Exception as e:
                logger.warning(f"   ⚠️  生成 pycode 失败: {e}")
                import traceback
                logger.debug(traceback.format_exc())
            
        except Exception as e:
            error_count += 1
            logger.error(f"   ❌ 构建失败: {e}")
            import traceback
            logger.debug(traceback.format_exc())
            continue
    
    # 6. 汇总结果
    logger.info("\n" + "="*60)
    logger.info("📊 构建结果汇总")
    logger.info("="*60)
    logger.info(f"总工具数: {len(tool_records)}")
    logger.info(f"✅ 成功构建: {success_count}")
    logger.info(f"💾 使用缓存: {cache_count}")
    logger.info(f"❌ 构建失败: {error_count}")
    logger.info(f"📦 最终 Fake Tools: {len(fake_tools)}")
    
    return fake_tools


async def main():
    """主函数"""
    import argparse
    
    parser = argparse.ArgumentParser(
        description="批量构建 Fake Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 构建 Fake Tools
  python build_fake_tools_batch.py \\
    --context-file /data/johndoe/MCP-Personal/mcp_configs/lark-mcp/context-test/context.json \\
    --tool-call-dir /data/johndoe/MCP-Personal/mcp_configs/lark-mcp/context-test \\
    --tools-description-file /data/johndoe/MCP-Personal/mcp_configs/lark-mcp/tools_description.json
  
  # 强制重新构建（忽略缓存）
  python build_fake_tools_batch.py \\
    --context-file /data/johndoe/MCP-Personal/mcp_configs/lark-mcp/context-test/context.json \\
    --tool-call-dir /data/johndoe/MCP-Personal/mcp_configs/lark-mcp/context-test \\
    --tools-description-file /data/johndoe/MCP-Personal/mcp_configs/lark-mcp/tools_description.json \\
    --force
        """
    )
    
    parser.add_argument(
        "--context-file",
        type=str,
        required=True,
        help="Context 文件路径（例如: /data/johndoe/MCP-Personal/mcp_configs/lark-mcp/context-test/context.json）"
    )
    
    parser.add_argument(
        "--tool-call-dir",
        type=str,
        required=True,
        help="工具调用记录保存的文件夹（例如: /data/johndoe/MCP-Personal/mcp_configs/lark-mcp/context-test）"
    )
    
    parser.add_argument(
        "--tools-description-file",
        type=str,
        required=True,
        help="工具描述文件路径（例如: /data/johndoe/MCP-Personal/mcp_configs/lark-mcp/tools_description.json）"
    )
    
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="不使用缓存，强制重新构建所有 Fake Tools"
    )
    
    parser.add_argument(
        "--no-llm-analysis",
        action="store_true",
        help="不使用 LLM 分析错误模式（构建更快但可能不够准确）"
    )
    
    parser.add_argument(
        "--force",
        action="store_true",
        help="强制重新构建（忽略缓存，等同于 --no-cache）"
    )
    
    args = parser.parse_args()
    
    context_file = Path(args.context_file)
    tool_call_dir = Path(args.tool_call_dir)
    tools_description_file = Path(args.tools_description_file)
    
    try:
        fake_tools = await build_fake_tools(
            context_file=context_file,
            tool_call_dir=tool_call_dir,
            tools_description_file=tools_description_file,
            use_cache=not (args.no_cache or args.force),
            use_llm_analysis=not args.no_llm_analysis,
            force_rebuild=args.force
        )
        
        if fake_tools:
            logger.info(f"\n✅ 成功构建 {len(fake_tools)} 个 Fake Tools")
            logger.info("\n构建的 Fake Tools:")
            for tool_name in sorted(fake_tools.keys()):
                logger.info(f"   - {tool_name}")
        else:
            logger.warning("\n⚠️  未构建任何 Fake Tools")
            sys.exit(1)
            
    except Exception as e:
        logger.error(f"\n❌ 程序执行失败: {e}")
        import traceback
        logger.error(traceback.format_exc())
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
