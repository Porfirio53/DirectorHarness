#!/usr/bin/env python3
"""
将amap格式的工具调用结果转换为lark格式
支持单文件转换和批量转换
"""

import json
import sys
import os
import glob

def parse_aws_result_string(result_string):
    """
    解析AWS格式的result字符串，提取meta、content、structuredContent、isError等信息
    """
    import ast

    # 初始化默认值
    parsed_result = {
        "meta": None,
        "content": [],
        "structuredContent": None,
        "isError": False
    }

    try:
        # 查找isError信息
        if "isError=True" in result_string:
            parsed_result["isError"] = True
        elif "isError=False" in result_string:
            parsed_result["isError"] = False

        # 提取meta部分
        meta_start = result_string.find("meta=")
        meta_end = result_string.find(" content=")
        if meta_start != -1 and meta_end != -1:
            meta_part = result_string[meta_start + 5:meta_end].strip()
            if meta_part == "None":
                parsed_result["meta"] = None
            else:
                # 尝试解析meta值
                try:
                    parsed_result["meta"] = ast.literal_eval(meta_part)
                except:
                    parsed_result["meta"] = meta_part

        # 提取content部分
        content_start = result_string.find("content=[")
        if content_start != -1:
            content_part = result_string[content_start:]
            # 找到匹配的 ]
            bracket_count = 0
            content_end = content_start
            for i, char in enumerate(content_part):
                if char == '[':
                    bracket_count += 1
                elif char == ']':
                    bracket_count -= 1
                    if bracket_count == 0:
                        content_end = content_start + i + 1
                        break

            if content_end > content_start:
                content_str = content_part[content_start:content_end]
                # 使用ast.literal_eval解析content
                try:
                    parsed_content = ast.literal_eval(content_str[9:-1])  # 去掉content=和最外层的[]
                    parsed_result["content"] = parsed_content if isinstance(parsed_content, list) else [parsed_content]
                except:
                    # 如果解析失败，保持原始字符串
                    parsed_result["content"] = [{"type": "text", "text": content_str, "annotations": None, "meta": None}]

        # 提取structuredContent部分
        structured_start = result_string.find("structuredContent=")
        if structured_start != -1:
            structured_part = result_string[structured_start + 18:]
            # 找到下一个空格或结尾
            structured_end = len(result_string)
            if " isError=" in structured_part:
                structured_end = structured_part.find(" isError=")

            structured_str = structured_part[:structured_end]
            if structured_str == "None":
                parsed_result["structuredContent"] = None
            else:
                try:
                    parsed_result["structuredContent"] = ast.literal_eval(structured_str)
                except:
                    parsed_result["structuredContent"] = structured_str

    except Exception as e:
        print(f"解析result字符串时出错: {e}")
        # 如果解析失败，至少保留isError信息
        if "isError=True" in result_string:
            parsed_result["isError"] = True
        elif "isError=False" in result_string:
            parsed_result["isError"] = False

        # 将原始字符串作为content
        parsed_result["content"] = [{"type": "text", "text": result_string, "annotations": None, "meta": None}]

    return parsed_result

def convert_amap_to_lark(amap_file_path, tools_file_path, output_file_path):
    """
    转换amap格式到lark格式
    自动检测和处理不同的result格式
    """
    # 读取amap格式数据
    with open(amap_file_path, 'r', encoding='utf-8') as f:
        amap_data = json.load(f)

    # 读取工具定义信息
    with open(tools_file_path, 'r', encoding='utf-8') as f:
        tools_data = json.load(f)

    # 获取工具信息
    tool_name = amap_data["tool_name"]
    server = amap_data["server"]
    tool_key = f"{server}:{tool_name}"

    if tool_key not in tools_data:
        raise ValueError(f"Tool {tool_key} not found in tools file")

    tool_info = tools_data[tool_key]

    # 准备转换后的数据（数组格式）
    lark_format_data = []

    # 转换每一次调用记录
    for call_record in amap_data["call_history"]:
        result_data = call_record["result"]

        # 检测result格式并处理
        if isinstance(result_data, str):
            # 字符串格式（AWS风格）
            parsed_result = parse_aws_result_string(result_data)
        else:
            # 对象格式（amap风格）
            parsed_result = result_data

        converted_record = {
            "tool_name": tool_key,
            "original_name": tool_info["original_name"],
            "server": server,
            "tool_description": tool_info["description"],
            "input_schema": tool_info["input_schema"],
            "manual_input": {
                "parameters": {
                    "data": call_record["call_params"]
                }
            },
            "tool_call": {
                "parameters_used": {
                    "data": call_record["call_params"]
                },
                "success": not parsed_result.get("isError", False),
                "error": None if not parsed_result.get("isError", False) else "Tool call failed",
                "result": parsed_result
            },
            "timestamp": float(call_record["timestamp"])  # 转换为float格式
        }

        lark_format_data.append(converted_record)

    # 写入输出文件
    with open(output_file_path, 'w', encoding='utf-8') as f:
        json.dump(lark_format_data, f, ensure_ascii=False, indent=2)

    print(f"转换完成！")
    print(f"输入文件: {amap_file_path}")
    print(f"输出文件: {output_file_path}")
    print(f"转换了 {len(lark_format_data)} 条记录")

def batch_convert_directory(amap_dir_path, tools_file_path, output_dir_path=None):
    """
    批量转换目录下的所有amap格式文件
    """
    if output_dir_path is None:
        output_dir_path = amap_dir_path

    # 获取所有json文件（排除备份文件）
    json_files = glob.glob(os.path.join(amap_dir_path, "*.json"))
    json_files = [f for f in json_files if not f.endswith('.backup')]

    print(f"找到 {len(json_files)} 个JSON文件")

    converted_count = 0
    failed_count = 0

    for json_file in json_files:
        try:
            # 生成输出文件名
            base_name = os.path.splitext(os.path.basename(json_file))[0]
            output_file = os.path.join(output_dir_path, f"{base_name}_format.json")

            print(f"\n正在转换: {json_file}")
            convert_amap_to_lark(json_file, tools_file_path, output_file)
            converted_count += 1

        except Exception as e:
            print(f"转换失败 {json_file}: {str(e)}")
            failed_count += 1

    print(f"\n批量转换完成！")
    print(f"成功转换: {converted_count} 个文件")
    print(f"转换失败: {failed_count} 个文件")

if __name__ == "__main__":
    # 路径配置
    amap_dir = "./MCP-Persona/mcp_context/xiaohongshu/real_context"
    tools_file = "./MCP-Persona/mcp_configs/all-server-tools/xiaohongshu.json"

    if len(sys.argv) > 1:
        # 命令行参数：单个文件
        amap_file = sys.argv[1]
        if len(sys.argv) > 2:
            output_file = sys.argv[2]
        else:
            base_name = os.path.splitext(os.path.basename(amap_file))[0]
            output_file = os.path.join(os.path.dirname(amap_file), f"{base_name}_lark.json")

        convert_amap_to_lark(amap_file, tools_file, output_file)
    else:
        # 批量转换整个目录
        batch_convert_directory(amap_dir, tools_file)