"""消息清洗与统计计算（移植自 astrbot message_cleaner / statistics 系列）

直接基于 OneBot 原始消息 dict 计算，消息形如：
{message_id, time, sender:{user_id, nickname, card}, message:[{type, data}]}
"""

import re
from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Tuple

from .models import ActivityVisualization, EmojiStatistics, GroupStatistics

COMMAND_PATTERN = re.compile(r"^\s*/")
CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def get_msg_text(msg: dict) -> str:
    """提取消息纯文本"""
    parts = []
    for seg in msg.get("message", []) or []:
        if isinstance(seg, dict) and seg.get("type") == "text":
            parts.append(str((seg.get("data") or {}).get("text", "")))
    return "".join(parts)


def get_sender_display_name(msg: dict) -> str:
    sender = msg.get("sender") or {}
    return str(sender.get("card") or sender.get("nickname") or sender.get("user_id") or "")


def get_reply_id(msg: dict):
    for seg in msg.get("message", []) or []:
        if isinstance(seg, dict) and seg.get("type") == "reply":
            return (seg.get("data") or {}).get("id")
    return None


def _is_emoji_like_image(data: dict) -> bool:
    sub_type = data.get("sub_type", data.get("subType"))
    if sub_type is not None:
        return str(sub_type) == "1"
    summary = str(data.get("summary", ""))
    return "动画表情" in summary or "表情" in summary


def count_msg_emojis(msg: dict, emoji_stats: EmojiStatistics = None) -> int:
    """统计单条消息表情数，可选累计到 emoji_stats"""
    count = 0
    for seg in msg.get("message", []) or []:
        if not isinstance(seg, dict):
            continue
        seg_type = seg.get("type")
        data = seg.get("data") or {}
        if seg_type == "face":
            count += 1
            if emoji_stats is not None:
                emoji_stats.face_count += 1
                key = f"emoji_{data.get('id', 'unknown')}"
                emoji_stats.face_details[key] = emoji_stats.face_details.get(key, 0) + 1
        elif seg_type == "mface":
            count += 1
            if emoji_stats is not None:
                emoji_stats.mface_count += 1
        elif seg_type == "bface":
            count += 1
            if emoji_stats is not None:
                emoji_stats.bface_count += 1
        elif seg_type == "sface":
            count += 1
            if emoji_stats is not None:
                emoji_stats.sface_count += 1
        elif seg_type == "image" and _is_emoji_like_image(data):
            count += 1
            if emoji_stats is not None:
                emoji_stats.mface_count += 1
    return count


def clean_messages(raw_messages: List[dict], bot_self_ids: List[str], filter_commands: bool = True) -> List[dict]:
    """清洗消息：过滤机器人消息、指令消息、无意义消息"""
    bot_ids = {str(x).strip() for x in bot_self_ids if str(x).strip()}
    cleaned = []
    for msg in raw_messages:
        if not isinstance(msg, dict):
            continue
        sender = msg.get("sender") or {}
        user_id = str(sender.get("user_id", ""))
        if user_id in bot_ids:
            continue
        text = get_msg_text(msg)
        if filter_commands and text and COMMAND_PATTERN.match(text):
            continue
        # 有意义判定：有非空文本或有非 reply 的其他段
        has_meaningful = bool(text.strip())
        if not has_meaningful:
            for seg in msg.get("message", []) or []:
                if isinstance(seg, dict) and seg.get("type") not in ("reply", "text"):
                    has_meaningful = True
                    break
        if has_meaningful:
            cleaned.append(msg)
    return cleaned


def calculate_group_statistics(messages: List[dict]) -> GroupStatistics:
    """计算群基础统计"""
    total_chars = 0
    participants = set()
    hour_counts = defaultdict(int)
    daily_counts = defaultdict(int)
    emoji_stats = EmojiStatistics()

    for msg in messages:
        sender = msg.get("sender") or {}
        participants.add(str(sender.get("user_id", "")))
        msg_time = datetime.fromtimestamp(msg.get("time", 0))
        hour_counts[msg_time.hour] += 1
        daily_counts[msg_time.strftime("%Y-%m-%d")] += 1
        total_chars += len(get_msg_text(msg))
        count_msg_emojis(msg, emoji_stats)

    most_active_hour = max(hour_counts.items(), key=lambda x: x[1])[0] if hour_counts else 0
    most_active_period = f"{most_active_hour:02d}:00-{(most_active_hour + 1) % 24:02d}:00"

    sorted_hours = sorted(hour_counts.items(), key=lambda x: x[1], reverse=True)
    peak_hours = [h for h, _ in sorted_hours[:3]]

    viz = ActivityVisualization(
        hourly_activity=dict(hour_counts),
        daily_activity=dict(daily_counts),
        peak_hours=peak_hours,
    )

    return GroupStatistics(
        message_count=len(messages),
        total_characters=total_chars,
        participant_count=len(participants),
        most_active_period=most_active_period,
        golden_quotes=[],
        emoji_count=emoji_stats.total_emoji_count,
        emoji_statistics=emoji_stats,
        activity_visualization=viz,
    )


def analyze_user_activity(messages: List[dict], bot_self_ids: List[str]) -> Dict[str, dict]:
    """计算每用户活动统计"""
    bot_ids = {str(x).strip() for x in bot_self_ids if str(x).strip()}
    user_stats: Dict[str, dict] = {}
    for msg in messages:
        sender = msg.get("sender") or {}
        user_id = str(sender.get("user_id", ""))
        if not user_id or user_id in bot_ids:
            continue
        stats = user_stats.setdefault(
            user_id,
            {
                "message_count": 0,
                "char_count": 0,
                "emoji_count": 0,
                "nickname": "",
                "hours": {},
                "reply_count": 0,
            },
        )
        stats["message_count"] += 1
        stats["char_count"] += len(get_msg_text(msg))
        stats["emoji_count"] += count_msg_emojis(msg)
        stats["nickname"] = get_sender_display_name(msg) or stats["nickname"]
        hour = datetime.fromtimestamp(msg.get("time", 0)).hour
        stats["hours"][hour] = stats["hours"].get(hour, 0) + 1
        if get_reply_id(msg):
            stats["reply_count"] += 1
    return user_stats


def get_top_users(user_stats: Dict[str, dict], limit: int = 10, min_messages: int = 5) -> List[dict]:
    eligible = [
        {"user_id": uid, **stats}
        for uid, stats in user_stats.items()
        if stats.get("message_count", 0) >= min_messages
    ]
    eligible.sort(key=lambda x: x["message_count"], reverse=True)
    return eligible[:limit]


def get_hourly_chart_data(hourly_activity: dict) -> List[dict]:
    """24 小时活跃图表数据"""
    # 兼容字符串键
    hourly = {int(k): v for k, v in (hourly_activity or {}).items()}
    max_activity = max(hourly.values()) if hourly else 1
    chart_data = []
    for hour in range(24):
        count = hourly.get(hour, 0)
        percentage = (count / max_activity) * 100 if max_activity > 0 else 0
        chart_data.append({"hour": hour, "count": count, "percentage": round(percentage, 1)})
    return chart_data


def _build_member_name_map(messages: List[dict]) -> Dict[str, str]:
    names: Dict[str, str] = {}
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        sender = msg.get("sender") or {}
        user_id = str(sender.get("user_id") or "").strip()
        name = str(sender.get("card") or sender.get("nickname") or "").strip()
        if user_id and name:
            names[user_id] = name
    return names


def _at_display_name(at_id, member_names: Dict[str, str]) -> str:
    text = str(at_id or "").strip()
    if not text:
        return ""
    if text.lower() == "all":
        return "全体成员"
    return member_names.get(text) or text


def build_llm_text_messages(messages: List[dict], bot_self_ids: List[str]) -> List[dict]:
    """提取用于 LLM 分析的文本消息: [{sender, time, content, user_id}]"""
    bot_ids = {str(x).strip() for x in bot_self_ids if str(x).strip()}
    member_names = _build_member_name_map(messages)
    text_messages = []
    for msg in messages:
        sender = msg.get("sender") or {}
        user_id = str(sender.get("user_id", ""))
        if not user_id or user_id in bot_ids:
            continue
        text_parts = []
        for seg in msg.get("message", []) or []:
            if not isinstance(seg, dict):
                continue
            seg_type = seg.get("type")
            data = seg.get("data") or {}
            if seg_type == "text":
                t = str(data.get("text", "")).strip()
                if t:
                    text_parts.append(t)
            elif seg_type == "at":
                at_id = data.get("qq") or data.get("id")
                at_name = _at_display_name(at_id, member_names)
                if at_name:
                    text_parts.append(f"@{at_name}")
            elif seg_type == "reply":
                rid = data.get("id")
                if rid:
                    text_parts.append(f"[回复:{rid}]")
        combined = "".join(text_parts).strip()
        if not combined or len(combined) <= 2 or combined.startswith("/"):
            continue
        cleaned = combined.replace("“", '"').replace("”", '"')
        cleaned = cleaned.replace("‘", "'").replace("’", "'")
        cleaned = cleaned.replace("\n", " ").replace("\r", " ").replace("\t", " ")
        cleaned = CONTROL_CHARS.sub("", cleaned)
        text_messages.append(
            {
                "sender": get_sender_display_name(msg),
                "time": datetime.fromtimestamp(msg.get("time", 0)).strftime("%H:%M"),
                "content": cleaned,
                "user_id": user_id,
            },
        )
    return text_messages
