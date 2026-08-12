#!/usr/bin/env python3
"""
使用真实工具调用记录测试 Fake Tool
给定MCP文件夹，读取每个真实工具调用记录，用相同的输入调用对应的模拟工具，获得结果并保存
"""

import asyncio
import json
import logging
import pickle
import sys
import re
from pathlib import Path
from typing import Dict, Any, List, Optional

from fake_tool.python_code_simulator import PythonCodeSimulator
from fake_tool.llm_client import LLMClient

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("test_fake_tool_with_real_records")

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
}

EXCLUDED_PREFIXES = {
    "fake_",  # Fake Tool 使用记录
    "fake_tool_cache_",  # Fake Tool 缓存
}


def get_safe_tool_name(tool_name: str) -> str:
    """将工具名称转换为安全的文件名"""
    return tool_name.replace(":", "_").replace("/", "_").replace("\\", "_")


def get_mcp_name_from_path(mcp_dir: Path) -> str:
    """从路径中提取MCP名称"""
    return mcp_dir.name


def get_mcp_configs_dir(mcp_dir: Path) -> Path:
    """获取MCP配置根目录"""
    return mcp_dir.parent


def find_tool_record_files(mcp_dir: Path) -> Dict[str, Path]:
    """查找所有工具记录文件"""
    tool_records = {}
    
    for file_path in mcp_dir.glob("*.json"):
        file_name = file_path.name
        
        # 排除配置文件
        if file_name in EXCLUDED_FILES:
            continue
        
        # 排除以特定前缀开头的文件
        if any(file_name.startswith(prefix) for prefix in EXCLUDED_PREFIXES):
            continue
        
        # 尝试从文件中提取工具名称
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            
            # 支持数组或单个对象格式
            if isinstance(data, list) and len(data) > 0:
                tool_name = data[0].get("tool_name")
            elif isinstance(data, dict):
                tool_name = data.get("tool_name")
            else:
                continue
            
            if tool_name:
                tool_records[tool_name] = file_path
        except Exception as e:
            logger.warning(f"读取文件失败 {file_path}: {e}")
            continue
    
    return tool_records


def load_pycode_simulator(
    mcp_dir: Path,
    tool_name: str
) -> Optional[PythonCodeSimulator]:
    """加载 pycode 模拟器"""
    safe_tool_name = get_safe_tool_name(tool_name)
    pycode_dir = mcp_dir / "pycode"
    pycode_path = pycode_dir / f"{safe_tool_name}.py"
    
    if not pycode_path.exists():
        logger.warning(f"⚠️  pycode 文件不存在: {pycode_path}")
        return None
    
    try:
        simulator = PythonCodeSimulator(pycode_path=pycode_path)
        logger.info(f"✅ 加载 pycode: {tool_name}")
        return simulator
    except Exception as e:
        logger.error(f"❌ 加载 pycode 失败 {tool_name}: {e}")
        import traceback
        logger.debug(traceback.format_exc())
        return None


async def is_success_response_with_llm(
    llm_client: LLMClient,
    tool_name: str,
    tool_description: str,
    input_schema: Dict[str, Any],
    input_parameters: Dict[str, Any],
    tool_call: Dict[str, Any]
) -> bool:
    """使用 LLM 判断工具调用是否成功"""
    # 将结果转换为字符串
    result_str = json.dumps(tool_call, indent=2, ensure_ascii=False)
    
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
  "reason": "brief explanation"
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
                return is_success
            except json.JSONDecodeError:
                logger.warning(f"解析LLM响应失败: {response[:200]}")
        
        # 如果解析失败，回退判断
        logger.warning("LLM判断失败，使用回退判断")
        return _is_success_fallback(tool_call)
        
    except Exception as e:
        logger.warning(f"LLM判断失败: {e}，使用回退判断")
        return _is_success_fallback(tool_call)


def _is_success_fallback(tool_call: Dict[str, Any]) -> bool:
    """回退判断（基于输出内容）"""
    if not isinstance(tool_call, dict):
        return False
    
    # 检查 result 中的 isError 字段
    result = tool_call.get("result")
    if isinstance(result, dict):
        if result.get("isError", False) is True:
            return False
        
        # 检查是否有实际内容
        content = result.get("content", [])
        if content and isinstance(content, list):
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text", "")
                    if text:
                        try:
                            content_data = json.loads(text)
                            if isinstance(content_data, dict):
                                # 检查错误码
                                if "code" in content_data:
                                    code = content_data.get("code")
                                    if code and code != 0:
                                        return False
                                # 检查错误字段
                                if "error" in content_data:
                                    return False
                        except (json.JSONDecodeError, TypeError):
                            pass
            # 有内容且没有错误指示，认为是成功
            return True
    
    return False


def load_real_records(record_file: Path) -> List[Dict[str, Any]]:
    """加载真实工具调用记录"""
    try:
        with open(record_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        # 支持数组或单个对象格式
        if isinstance(data, list):
            return data
        elif isinstance(data, dict):
            return [data]
        else:
            logger.warning(f"记录文件格式不正确: {record_file}")
            return []
    except Exception as e:
        logger.error(f"加载记录文件失败 {record_file}: {e}")
        return []


async def test_fake_tool_with_records(
    mcp_dir: str,
    output_dir: Optional[str] = None
):
    """使用真实工具调用记录测试Fake Tool"""
    
    mcp_dir = Path(mcp_dir)
    if not mcp_dir.exists():
        logger.error(f"❌ MCP文件夹不存在: {mcp_dir}")
        return
    
    mcp_name = get_mcp_name_from_path(mcp_dir)
    logger.info(f"📂 处理 MCP: {mcp_name}")
    logger.info(f"📁 目录: {mcp_dir}")
    
    # 设置输出目录
    if output_dir is None:
        output_dir = mcp_dir
    else:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
    
    # 1. 查找所有工具记录文件
    logger.info("\n" + "="*60)
    logger.info("🔍 查找工具记录文件...")
    logger.info("="*60)
    
    tool_records = find_tool_record_files(mcp_dir)
    
    if not tool_records:
        logger.warning("❌ 未找到任何工具记录文件")
        return
    
    logger.info(f"✅ 找到 {len(tool_records)} 个工具记录文件:")
    for tool_name, record_file in tool_records.items():
        logger.info(f"   - {tool_name} ({record_file.name})")
    
    # 2. 检查 pycode 目录
    logger.info("\n" + "="*60)
    logger.info("🔍 检查 pycode 目录...")
    logger.info("="*60)
    
    pycode_dir = mcp_dir / "pycode"
    if not pycode_dir.exists():
        logger.error(f"❌ pycode 目录不存在: {pycode_dir}")
        logger.error("   请先运行 build_fake_tools_batch.py 生成 pycode")
        return
    
    logger.info(f"✅ pycode 目录: {pycode_dir}")
    
    # 3. 处理每个工具
    logger.info("\n" + "="*60)
    logger.info("🧪 开始测试 Fake Tools...")
    logger.info("="*60)
    
    total_tools = 0
    success_tools = 0
    total_calls = 0
    success_calls = 0
    
    # 初始化 LLM 客户端用于判断真实调用是否成功
    llm_client = LLMClient(FAKE_TOOL_API_BASE_URL, FAKE_TOOL_API_KEY, FAKE_TOOL_MODEL)
    
    # 比较统计
    comparison_stats = {
        "both_success": 0,
        "both_failure": 0,
        "fake_success_real_failure": 0,
        "fake_failure_real_success": 0,
        "total_comparisons": 0
    }
    comparison_details = {
        "both_success": [],
        "both_failure": [],
        "fake_success_real_failure": [],
        "fake_failure_real_success": []
    }
    
    # 按文件名排序，确保顺序一致
    sorted_tool_records = sorted(tool_records.items(), key=lambda x: x[1].name)
    
    for tool_name, record_file in sorted_tool_records:
        total_tools += 1
        logger.info(f"\n{'='*60}")
        logger.info(f"🔧 处理工具: {tool_name}")
        logger.info(f"📄 记录文件: {record_file.name}")
        logger.info(f"{'='*60}")
        
        # 加载真实工具调用记录
        real_records = load_real_records(record_file)
        if not real_records:
            logger.warning(f"⚠️  未找到有效记录，跳过")
            continue
        
        logger.info(f"📊 找到 {len(real_records)} 条真实调用记录")
        
        # 加载 pycode 模拟器
        simulator = load_pycode_simulator(mcp_dir, tool_name)
        if not simulator:
            logger.warning(f"⚠️  无法加载 pycode，跳过")
            continue
        
        success_tools += 1
        
        # 使用真实记录的输入参数调用Fake Tool
        fake_records = []
        
        for i, real_record in enumerate(real_records, 1):
            total_calls += 1
            logger.info(f"\n  📝 处理记录 {i}/{len(real_records)}")
            
            # 提取输入参数
            tool_call = real_record.get("tool_call", {})
            if not isinstance(tool_call, dict):
                logger.warning(f"    ⚠️  记录格式不正确，跳过")
                continue
            
            # 获取输入参数，如果不存在则使用空字典（空参数是合法的）
            parameters_used = tool_call.get("parameters_used")
            if parameters_used is None:
                # 如果parameters_used字段不存在，尝试从manual_input获取
                manual_input = real_record.get("manual_input", {})
                if isinstance(manual_input, dict):
                    parameters_used = manual_input.get("parameters", {})
                else:
                    parameters_used = {}
            
            # 确保parameters_used是字典类型（空字典也是合法的）
            if not isinstance(parameters_used, dict):
                logger.warning(f"    ⚠️  输入参数格式不正确（不是字典），跳过")
                continue
            
            # 显示输入参数（空字典也显示）
            if parameters_used:
                logger.info(f"    📥 输入参数: {json.dumps(parameters_used, indent=2, ensure_ascii=False)[:200]}...")
            else:
                logger.info(f"    📥 输入参数: {{}} (空参数)")
            
            # 调用 pycode 模拟器
            try:
                fake_response = await simulator.simulate(parameters_used)
                success_calls += 1
                
                # 获取真实调用的结果用于比较
                real_tool_call = tool_call
                
                # 使用 LLM 判断真实调用是否成功
                tool_description = real_record.get("tool_description", "")
                input_schema = real_record.get("input_schema", {})
                
                try:
                    real_is_success = await is_success_response_with_llm(
                        llm_client=llm_client,
                        tool_name=tool_name,
                        tool_description=tool_description,
                        input_schema=input_schema,
                        input_parameters=parameters_used,
                        tool_call=real_tool_call
                    )
                except Exception as e:
                    logger.warning(f"   LLM 判断真实调用失败: {e}，使用回退判断")
                    real_is_success = _is_success_fallback(real_tool_call)
                
                # 判断模拟调用是否成功（基于 success 字段）
                fake_is_success = fake_response.get("success", False)
                
                # 统计比较结果
                comparison_stats["total_comparisons"] += 1
                
                if real_is_success and fake_is_success:
                    comparison_stats["both_success"] += 1
                    match_status = "✅"
                    comparison_key = "both_success"
                elif not real_is_success and not fake_is_success:
                    comparison_stats["both_failure"] += 1
                    match_status = "✅"
                    comparison_key = "both_failure"
                elif fake_is_success and not real_is_success:
                    comparison_stats["fake_success_real_failure"] += 1
                    match_status = "⚠️"
                    comparison_key = "fake_success_real_failure"
                else:  # not fake_is_success and real_is_success
                    comparison_stats["fake_failure_real_success"] += 1
                    match_status = "⚠️"
                    comparison_key = "fake_failure_real_success"
                
                logger.info(f"    {match_status} pycode 调用成功")
                if match_status == "⚠️":
                    logger.info(f"      真实: {'成功' if real_is_success else '失败'}")
                    logger.info(f"      模拟: {'成功' if fake_is_success else '失败'}")
                
                # 添加到比较详情
                comparison_details[comparison_key].append({
                    "tool_name": tool_name,
                    "input_parameters": parameters_used,
                    "fake_tool_output": fake_response,
                    "real_tool_output": real_tool_call
                })
                
                # 构建Fake Tool调用记录
                fake_record = {
                    "tool_name": tool_name,
                    "original_name": real_record.get("original_name", ""),
                    "server": real_record.get("server", ""),
                    "tool_description": real_record.get("tool_description", ""),
                    "input_schema": real_record.get("input_schema", {}),
                    "manual_input": {
                        "parameters": parameters_used
                    },
                    "tool_call": fake_response,
                    "real_tool_call": real_tool_call,  # 保存真实调用用于比较
                    "real_is_success": real_is_success,  # LLM 判断的真实调用结果
                    "fake_is_success": fake_is_success,  # 模拟调用结果
                    "match_status": match_status,
                    "timestamp": real_record.get("timestamp", ""),
                    "source": "pycode_simulation"
                }
                
                fake_records.append(fake_record)
                
            except Exception as e:
                logger.error(f"    ❌ pycode 调用失败: {e}")
                import traceback
                logger.debug(traceback.format_exc())
                continue
        
        # 保存Fake Tool调用记录
        if fake_records:
            safe_tool_name = get_safe_tool_name(tool_name)
            output_file = output_dir / f"fake_{safe_tool_name}.json"
            
            try:
                with open(output_file, "w", encoding="utf-8") as f:
                    json.dump(fake_records, f, indent=2, ensure_ascii=False)
                
                logger.info(f"\n  💾 已保存 Fake Tool 调用记录: {output_file}")
                logger.info(f"  📊 成功调用: {len(fake_records)}/{len(real_records)}")
            except Exception as e:
                logger.error(f"  ❌ 保存失败: {e}")
    
    # 保存比较结果
    comparison_output_dir = mcp_dir / "fake_tool_using"
    comparison_output_dir.mkdir(parents=True, exist_ok=True)
    comparison_file = comparison_output_dir / "comparison.json"
    
    comparison_data = {
        "summary": comparison_stats,
        "details": comparison_details
    }
    
    try:
        with open(comparison_file, "w", encoding="utf-8") as f:
            json.dump(comparison_data, f, indent=2, ensure_ascii=False)
        logger.info(f"\n💾 已保存比较结果: {comparison_file}")
    except Exception as e:
        logger.error(f"❌ 保存比较结果失败: {e}")
    
    # 输出总结
    logger.info("\n" + "="*60)
    logger.info("📊 测试总结")
    logger.info("="*60)
    logger.info(f"总工具数: {total_tools}")
    logger.info(f"成功加载工具: {success_tools}")
    logger.info(f"总调用次数: {total_calls}")
    logger.info(f"成功调用次数: {success_calls}")
    logger.info(f"成功率: {success_calls/total_calls*100:.1f}%" if total_calls > 0 else "N/A")
    logger.info("\n📊 比较统计:")
    logger.info(f"  都是成功: {comparison_stats['both_success']}")
    logger.info(f"  都是失败: {comparison_stats['both_failure']}")
    logger.info(f"  模拟成功但真实失败: {comparison_stats['fake_success_real_failure']}")
    logger.info(f"  模拟失败但真实成功: {comparison_stats['fake_failure_real_success']}")
    logger.info(f"  总比较次数: {comparison_stats['total_comparisons']}")
    logger.info("="*60)


def main():
    """主函数"""
    if len(sys.argv) < 2:
        print("用法: python test_fake_tool_with_real_records.py <MCP文件夹路径> [输出目录]")
        print("\n示例:")
        print("  python test_fake_tool_with_real_records.py /Users/JohnDoe/Documents/MCP-Personal/mcp_configs/feishu")
        print("  python test_fake_tool_with_real_records.py /Users/JohnDoe/Documents/MCP-Personal/mcp_configs/feishu ./output")
        sys.exit(1)
    
    mcp_dir = sys.argv[1]
    output_dir = sys.argv[2] if len(sys.argv) > 2 else None
    
    asyncio.run(test_fake_tool_with_records(mcp_dir, output_dir))


if __name__ == "__main__":
    main()

