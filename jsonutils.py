"""LLM 响应 JSON 解析与正则降级工具（移植自 astrbot 插件 json_utils.py）"""

import json
import re
from typing import List, Optional, Tuple

from nekro_agent.api.core import logger


def _clean_json_string(text: str) -> str:
    return text.replace('\\"', '"').replace("\\n", " ").replace("\\t", " ")


def fix_json(text: str) -> str:
    """尝试修复 LLM 输出中常见的 JSON 错误"""
    text = re.sub(r"```json\s*", "", text)
    text = re.sub(r"```\s*$", "", text)
    text = text.replace("\n", " ").replace("\r", " ")
    text = re.sub(r"\s+", " ", text)

    # 中文标点替换
    text = text.replace("“", '"').replace("”", '"')
    text = text.replace("‘", "'").replace("’", "'")
    text = text.replace("，", ",")
    text = text.replace("：", ":")
    text = text.replace("（", "(").replace("）", ")")

    # 截断修复：数组未闭合
    stripped = text.strip()
    if stripped.startswith("[") and not stripped.endswith("]"):
        last_complete = text.rfind("}")
        if last_complete > 0:
            text = text[: last_complete + 1] + "]"

    # 缺失逗号 / 字段名引号 / 多余逗号
    text = re.sub(r"}\s*{", "}, {", text)

    def quote_field_names(match):
        return f'{match.group(1)}"{match.group(2)}":'

    text = re.sub(r"([{,]\s*)([a-zA-Z_][a-zA-Z0-9_]*)\s*:", quote_field_names, text)
    text = re.sub(r",\s*}", "}", text)
    text = re.sub(r",\s*]", "]", text)
    return text


def _parse_json_with_pattern(
    result_text: str, pattern: str, data_type: str,
) -> Tuple[bool, Optional[object], Optional[str]]:
    clean_text = re.sub(r"```(?:json)?\s*", "", (result_text or "").strip())
    json_match = re.search(pattern, clean_text, re.DOTALL)
    if not json_match:
        return False, None, f"{data_type}: 响应中未找到 JSON 结构"
    json_text = json_match.group()
    try:
        return True, json.loads(json_text), None
    except json.JSONDecodeError:
        pass
    fixed = fix_json(json_text)
    fixed_match = re.search(pattern, fixed, re.DOTALL)
    if fixed_match:
        try:
            return True, json.loads(fixed_match.group()), None
        except json.JSONDecodeError as e:
            return False, None, f"{data_type}: JSON 修复后仍解析失败: {e}"
    return False, None, f"{data_type}: JSON 修复失败"


def parse_json_response(result_text: str, data_type: str) -> Tuple[bool, Optional[list], Optional[str]]:
    """解析 JSON 数组响应"""
    ok, data, err = _parse_json_with_pattern(result_text, r"\[.*\]", data_type)
    if ok and not isinstance(data, list):
        return False, None, f"{data_type}: 期望数组，得到 {type(data)}"
    return ok, data, err


def parse_json_object_response(result_text: str, data_type: str) -> Tuple[bool, Optional[dict], Optional[str]]:
    """解析 JSON 对象响应"""
    ok, data, err = _parse_json_with_pattern(result_text, r"\{.*\}", data_type)
    if ok and not isinstance(data, dict):
        return False, None, f"{data_type}: 期望对象，得到 {type(data)}"
    return ok, data, err


def extract_topics_with_regex(result_text: str, max_count: int) -> List[dict]:
    """正则降级提取话题"""
    topics = []
    pattern = (
        r'\{\s*"topic":\s*"([^"]*(?:\\.[^"]*)*)"\s*,\s*"contributors":\s*\[(.*?)\]\s*,'
        r'\s*"detail":\s*"([^"]*(?:\\.[^"]*)*)"\s*\}'
    )
    matches = re.findall(pattern, result_text, re.DOTALL)
    for match in matches[:max_count]:
        contributors = [
            c.strip().strip('"').strip("'")
            for c in match[1].split(",")
            if c.strip().strip('"').strip("'")
        ]
        topics.append(
            {
                "topic": _clean_json_string(match[0].strip()),
                "contributors": contributors or ["群友"],
                "detail": _clean_json_string(match[2].strip()),
            },
        )
    if topics:
        logger.info(f"[group_analysis] 正则降级提取到 {len(topics)} 个话题")
    return topics


def extract_user_titles_with_regex(result_text: str, max_count: int) -> List[dict]:
    """正则降级提取用户称号"""
    titles = []
    pattern = (
        r'\{\s*"name":\s*"([^"]*(?:\\.[^"]*)*)"\s*,\s*"user_id":\s*"?(\d+)"?\s*,'
        r'\s*"title":\s*"([^"]*(?:\\.[^"]*)*)"\s*,\s*"mbti":\s*"([^"]*)"\s*,'
        r'\s*"reason":\s*"([^"]*(?:\\.[^"]*)*)"\s*\}'
    )
    matches = re.findall(pattern, result_text, re.DOTALL)
    for match in matches[:max_count]:
        titles.append(
            {
                "name": _clean_json_string(match[0].strip()),
                "user_id": match[1].strip(),
                "title": _clean_json_string(match[2].strip()),
                "mbti": match[3].strip(),
                "reason": _clean_json_string(match[4].strip()),
            },
        )
    if titles:
        logger.info(f"[group_analysis] 正则降级提取到 {len(titles)} 个用户称号")
    return titles


def extract_golden_quotes_with_regex(result_text: str, max_count: int) -> List[dict]:
    """正则降级提取金句"""
    quotes = []
    pattern = (
        r'\{\s*"content":\s*"([^"]*(?:\\.[^"]*)*)"\s*,\s*"sender":\s*"([^"]*(?:\\.[^"]*)*)"\s*,'
        r'\s*"reason":\s*"([^"]*(?:\\.[^"]*)*)"\s*\}'
    )
    matches = re.findall(pattern, result_text, re.DOTALL)
    if not matches:
        pattern = (
            r'"content":\s*"([^"]*(?:\\.[^"]*)*)"[^}]*"sender":\s*"([^"]*(?:\\.[^"]*)*)"[^}]*'
            r'"reason":\s*"([^"]*(?:\\.[^"]*)*)"'
        )
        matches = re.findall(pattern, result_text, re.DOTALL)
    for match in matches[:max_count]:
        quotes.append(
            {
                "content": _clean_json_string(match[0].strip()),
                "sender": match[1].strip(),
                "reason": _clean_json_string(match[2].strip()),
            },
        )
    if quotes:
        logger.info(f"[group_analysis] 正则降级提取到 {len(quotes)} 条金句")
    return quotes
