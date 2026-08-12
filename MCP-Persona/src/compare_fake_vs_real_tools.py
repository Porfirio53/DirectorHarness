"""比较 Fake Tool 和真实工具的输出"""

import json
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional
import asyncio

logger = logging.getLogger(__name__)


def is_success_response_with_llm(tool_call: Dict[str, Any]) -> bool:
    """
    基于响应内容判断成功/失败（不依赖success字段）
    
    Args:
        tool_call: 工具调用结果字典
    
    Returns:
        True表示成功，False表示失败
    """
    if not isinstance(tool_call, dict):
        return False
    
    # 1. 检查 result 中的 isError 字段（这是最可靠的错误指示）
    result = tool_call.get("result")
    if isinstance(result, dict):
        if result.get("isError", False) is True:
            return False
        
        # 2. 检查 result.content 中的实际内容
        content = result.get("content", [])
        if content and isinstance(content, list):
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text", "")
                    if text:
                        try:
                            # 尝试解析 JSON 内容
                            content_data = json.loads(text)
                            # 检查是否有错误码或错误消息
                            if isinstance(content_data, dict):
                                if "code" in content_data:
                                    code = content_data.get("code")
                                    if code and code != 0:
                                        return False
                                if "error" in content_data:
                                    return False
                                if "msg" in content_data:
                                    msg = content_data.get("msg", "").lower()
                                    error_keywords = ["error", "invalid", "failed", "not found", "does not exist", "unauthorized", "forbidden"]
                                    if any(keyword in msg for keyword in error_keywords):
                                        return False
                        except (json.JSONDecodeError, TypeError):
                            # 如果不是JSON，检查文本内容
                            text_lower = text.lower()
                            error_keywords = ["error", "invalid", "failed", "not found", "does not exist", "unauthorized", "forbidden", "cannot", "unable"]
                            if any(keyword in text_lower for keyword in error_keywords):
        return False
    
    # 3. 检查 content 字段（如果 result 不存在）
    content = tool_call.get("content", [])
    if content and isinstance(content, list):
        for item in content:
            if isinstance(item, dict):
                text = item.get("text", "")
                if text:
                    try:
                        content_data = json.loads(text)
                        if isinstance(content_data, dict):
                            if "code" in content_data:
                                code = content_data.get("code")
                                if code and code != 0:
                                    return False
                            if "error" in content_data:
                                return False
                            if "msg" in content_data:
                                msg = content_data.get("msg", "").lower()
                                error_keywords = ["error", "invalid", "failed", "not found", "does not exist"]
                                if any(keyword in msg for keyword in error_keywords):
                                    return False
                    except (json.JSONDecodeError, TypeError):
                        text_lower = text.lower()
                        error_keywords = ["error", "invalid", "failed", "not found", "does not exist"]
                        if any(keyword in text_lower for keyword in error_keywords):
        return False
    
    # 4. 检查是否有实际数据返回（成功通常会有数据）
    # 如果 result 存在且有 content，且没有错误指示，认为是成功
    if result and isinstance(result, dict):
        content = result.get("content", [])
        if content and isinstance(content, list):
            # 有内容且没有错误指示，认为是成功
            return True
    
    # 5. 如果没有任何内容，可能是失败
    if not result and not content:
        return False
    
    return True


def _is_success_response_fallback(tool_call: Dict[str, Any]) -> bool:
    """基于响应内容判断（回退方案），不依赖success字段"""
    return is_success_response_with_llm(tool_call)


def compare_fake_vs_real(
    fake_tool_output: Dict[str, Any],
    real_tool_output: Dict[str, Any]
) -> Dict[str, Any]:
    """
    比较 Fake Tool 和真实工具的输出
    
    Returns:
        包含比较结果的字典
    """
    fake_success = is_success_response_with_llm(fake_tool_output)
    real_success = is_success_response_with_llm(real_tool_output)
    
    if fake_success and real_success:
        return {"status": "both_success"}
    elif not fake_success and not real_success:
        return {"status": "both_failure"}
    elif fake_success and not real_success:
        return {"status": "fake_success_real_failure"}
    else:
        return {"status": "fake_failure_real_success"}


def load_fake_tool_records(mcp_name: str, tool_name: str) -> List[Dict[str, Any]]:
    """加载 Fake Tool 使用记录"""
    safe_tool_name = tool_name.replace(":", "_").replace("/", "_")
    fake_records_path = Path("mcp_configs") / mcp_name / "fake_tool_using" / f"fake_{safe_tool_name}.json"
    
    if not fake_records_path.exists():
        logger.warning(f"Fake tool records not found: {fake_records_path}")
        return []
    
    try:
        with open(fake_records_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error loading fake tool records: {e}")
        return []


def load_real_tool_records(mcp_name: str, tool_name: str) -> List[Dict[str, Any]]:
    """加载真实工具使用记录"""
    safe_tool_name = tool_name.replace(":", "_").replace("/", "_")
    real_records_path = Path("mcp_configs") / mcp_name / f"{safe_tool_name}.json"
    
    if not real_records_path.exists():
        logger.warning(f"Real tool records not found: {real_records_path}")
        return []
    
    try:
        with open(real_records_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error loading real tool records: {e}")
        return []


def compare_all_tools(mcp_name: str) -> Dict[str, Any]:
    """比较所有工具的 Fake Tool 和真实工具输出"""
    
    mcp_config_dir = Path("mcp_configs") / mcp_name
    fake_tool_using_dir = mcp_config_dir / "fake_tool_using"
    
    if not fake_tool_using_dir.exists():
        logger.error(f"Fake tool using directory not found: {fake_tool_using_dir}")
        return {}
    
    # 获取所有 fake tool 记录文件
    fake_record_files = list(fake_tool_using_dir.glob("fake_*.json"))
    
    all_comparisons = {
            "both_success": [],
            "both_failure": [],
            "fake_success_real_failure": [],
            "fake_failure_real_success": []
        }
    
    for fake_record_file in fake_record_files:
        # 提取工具名称
        safe_tool_name = fake_record_file.stem.replace("fake_", "")
        tool_name = f"{mcp_name}:{safe_tool_name.replace('_', ':')}"
        
        # 加载记录
        fake_records = load_fake_tool_records(mcp_name, tool_name)
        real_records = load_real_tool_records(mcp_name, tool_name)
        
        # 匹配并比较
        for fake_record in fake_records:
            fake_input = fake_record.get("input_parameters", {})
            fake_output = fake_record.get("output", {})
        
            # 查找对应的真实工具记录
            for real_record in real_records:
                real_input = real_record.get("parameters_used", {})
        
                # 简单匹配：比较输入参数
                if fake_input == real_input:
                    comparison_result = compare_fake_vs_real(fake_output, real_record)
                    status = comparison_result["status"]
                    
                    all_comparisons[status].append({
                "tool_name": tool_name,
                        "input_parameters": fake_input,
                        "fake_tool_output": fake_output,
                        "real_tool_output": real_record
                    })
                    break
    
    # 生成摘要
    summary = {
        "both_success": len(all_comparisons["both_success"]),
        "both_failure": len(all_comparisons["both_failure"]),
        "fake_success_real_failure": len(all_comparisons["fake_success_real_failure"]),
        "fake_failure_real_success": len(all_comparisons["fake_failure_real_success"]),
        "total_comparisons": sum(len(v) for v in all_comparisons.values())
    }
    
    return {
        "summary": summary,
        "details": all_comparisons
    }


def main():
    """主函数"""
    import sys
    
    logging.basicConfig(level=logging.INFO)
    
    if len(sys.argv) < 2:
        print("Usage: python compare_fake_vs_real_tools.py <mcp_name>")
        sys.exit(1)
    
    mcp_name = sys.argv[1]
    
    print(f"比较 {mcp_name} 的 Fake Tool 和真实工具输出...")
    results = compare_all_tools(mcp_name)
    
    # 保存结果
    output_path = Path("mcp_configs") / mcp_name / "fake_tool_using" / "comparison.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    
    print(f"\n比较结果已保存到: {output_path}")
    print(f"\n摘要:")
    print(f"  双方成功: {results['summary']['both_success']}")
    print(f"  双方失败: {results['summary']['both_failure']}")
    print(f"  Fake成功但真实失败: {results['summary']['fake_success_real_failure']}")
    print(f"  Fake失败但真实成功: {results['summary']['fake_failure_real_success']}")
    print(f"  总计: {results['summary']['total_comparisons']}")


if __name__ == "__main__":
    main()
