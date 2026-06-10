"""增量分析（滑动窗口）：批次存储与合并

移植自 astrbot incremental_store / incremental_state / incremental_merge_service。
批次数据保存在 plugin.store（chat_key="global"）。
"""

import json
import time
import uuid
from typing import Dict, List, Optional, Tuple

from nekro_agent.api.core import logger

from .models import (
    ActivityVisualization,
    EmojiStatistics,
    GoldenQuote,
    GroupStatistics,
    QualityReview,
    SummaryTopic,
    TokenUsage,
)
from .plugin import store

TOPIC_SIM_THRESHOLD = 0.6
QUOTE_SIM_THRESHOLD = 0.7


def _index_key(group_id: str) -> str:
    return f"incr_index_{group_id}"


def _batch_key(group_id: str, batch_id: str) -> str:
    return f"incr_batch_{group_id}_{batch_id}"


def _last_ts_key(group_id: str) -> str:
    return f"incr_last_ts_{group_id}"


async def _get_json(key: str, default):
    raw = await store.get(chat_key="global", store_key=key)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


async def _set_json(key: str, value) -> None:
    await store.set(chat_key="global", store_key=key, value=json.dumps(value, ensure_ascii=False))


async def get_last_analyzed_ts(group_id: str) -> Optional[float]:
    raw = await store.get(chat_key="global", store_key=_last_ts_key(group_id))
    try:
        return float(raw) if raw else None
    except Exception:
        return None


async def set_last_analyzed_ts(group_id: str, ts: float) -> None:
    await store.set(chat_key="global", store_key=_last_ts_key(group_id), value=str(ts))


async def save_batch(group_id: str, batch: dict) -> None:
    batch_id = batch.get("batch_id") or uuid.uuid4().hex
    batch["batch_id"] = batch_id
    await _set_json(_batch_key(group_id, batch_id), batch)
    index = await _get_json(_index_key(group_id), [])
    index.append({"batch_id": batch_id, "timestamp": batch.get("timestamp", time.time())})
    await _set_json(_index_key(group_id), index)


async def query_batches(group_id: str, start_ts: float, end_ts: float) -> List[dict]:
    index = await _get_json(_index_key(group_id), [])
    batches = []
    for entry in index:
        ts = float(entry.get("timestamp", 0) or 0)
        if start_ts <= ts <= end_ts:
            batch = await _get_json(_batch_key(group_id, entry["batch_id"]), None)
            if batch:
                batches.append(batch)
    batches.sort(key=lambda b: b.get("timestamp", 0))
    return batches


async def cleanup_old_batches(group_id: str, before_ts: float) -> int:
    index = await _get_json(_index_key(group_id), [])
    keep, removed = [], 0
    for entry in index:
        if float(entry.get("timestamp", 0) or 0) < before_ts:
            try:
                await store.delete(chat_key="global", store_key=_batch_key(group_id, entry["batch_id"]))
            except Exception:
                pass
            removed += 1
        else:
            keep.append(entry)
    if removed:
        await _set_json(_index_key(group_id), keep)
    return removed


# ============ 合并 ============

def char_overlap_similarity(s1: str, s2: str) -> float:
    set1, set2 = set(s1 or ""), set(s2 or "")
    union = set1 | set2
    if not union:
        return 0.0
    return len(set1 & set2) / len(union)


def _is_duplicate_topic(topic: dict, existing: List[dict]) -> bool:
    return any(
        char_overlap_similarity(topic.get("topic", ""), e.get("topic", "")) >= TOPIC_SIM_THRESHOLD
        for e in existing
    )


def _is_duplicate_quote(quote: dict, existing: List[dict]) -> bool:
    return any(
        char_overlap_similarity(quote.get("content", ""), e.get("content", "")) >= QUOTE_SIM_THRESHOLD
        for e in existing
    )


def merge_batches(batches: List[dict]) -> dict:
    """合并多个批次为聚合状态"""
    state = {
        "total_analyses": len(batches),
        "total_messages": 0,
        "total_characters": 0,
        "hourly_msg_counts": {},
        "user_stats": {},
        "emoji_stats": {
            "face_count": 0, "mface_count": 0, "bface_count": 0,
            "sface_count": 0, "other_emoji_count": 0, "face_details": {},
        },
        "topics": [],
        "golden_quotes": [],
        "token_usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        "participant_ids": set(),
        "quality_reviews": [],
    }
    for batch in batches:
        state["total_messages"] += int(batch.get("messages_count", 0) or 0)
        state["total_characters"] += int(batch.get("characters_count", 0) or 0)
        for h, c in (batch.get("hourly_msg_counts") or {}).items():
            state["hourly_msg_counts"][str(h)] = state["hourly_msg_counts"].get(str(h), 0) + int(c)
        for uid, us in (batch.get("user_stats") or {}).items():
            agg = state["user_stats"].setdefault(
                uid,
                {"nickname": "", "message_count": 0, "char_count": 0, "emoji_count": 0, "reply_count": 0, "hours": {}},
            )
            agg["message_count"] += int(us.get("message_count", 0) or 0)
            agg["char_count"] += int(us.get("char_count", 0) or 0)
            agg["emoji_count"] += int(us.get("emoji_count", 0) or 0)
            agg["reply_count"] += int(us.get("reply_count", 0) or 0)
            agg["nickname"] = us.get("nickname") or agg["nickname"]
            for h, c in (us.get("hours") or {}).items():
                agg["hours"][str(h)] = agg["hours"].get(str(h), 0) + int(c)
        es = batch.get("emoji_stats") or {}
        for k in ("face_count", "mface_count", "bface_count", "sface_count", "other_emoji_count"):
            state["emoji_stats"][k] += int(es.get(k, 0) or 0)
        for fk, fc in (es.get("face_details") or {}).items():
            state["emoji_stats"]["face_details"][fk] = state["emoji_stats"]["face_details"].get(fk, 0) + int(fc)
        for topic in batch.get("topics") or []:
            if not _is_duplicate_topic(topic, state["topics"]):
                state["topics"].append(topic)
        for quote in batch.get("golden_quotes") or []:
            if not _is_duplicate_quote(quote, state["golden_quotes"]):
                state["golden_quotes"].append(quote)
        tu = batch.get("token_usage") or {}
        for k in state["token_usage"]:
            state["token_usage"][k] += int(tu.get(k, 0) or 0)
        state["participant_ids"].update(batch.get("participant_ids") or [])
        if batch.get("chat_quality_review"):
            state["quality_reviews"].append(batch["chat_quality_review"])
    return state


def get_state_summary(state: dict, window_desc: str) -> dict:
    hourly = {int(k): v for k, v in state["hourly_msg_counts"].items()}
    peak = sorted(hourly.items(), key=lambda x: x[1], reverse=True)[:3]
    return {
        "window": window_desc,
        "total_analyses": state["total_analyses"],
        "total_messages": state["total_messages"],
        "topics_count": len(state["topics"]),
        "quotes_count": len(state["golden_quotes"]),
        "participants": len(state["participant_ids"]),
        "peak_hours": ", ".join(f"{h:02d}:00({c})" for h, c in peak) or "无",
    }


def build_analysis_result(
    state: dict, user_titles: list, quality_review: Optional[QualityReview], template_name: str,
) -> Tuple[dict, Dict[str, str]]:
    """把合并状态转换为与全量分析一致的 analysis_result 结构"""
    hourly = {int(k): v for k, v in state["hourly_msg_counts"].items()}
    most_active_hour = max(hourly.items(), key=lambda x: x[1])[0] if hourly else 0
    es = state["emoji_stats"]
    emoji_statistics = EmojiStatistics(
        face_count=es["face_count"],
        mface_count=es["mface_count"],
        bface_count=es["bface_count"],
        sface_count=es["sface_count"],
        other_emoji_count=es["other_emoji_count"],
        face_details=dict(es["face_details"]),
    )
    golden_quotes = [GoldenQuote.from_dict(q) for q in state["golden_quotes"]]
    tu = state["token_usage"]
    stats = GroupStatistics(
        message_count=state["total_messages"],
        total_characters=state["total_characters"],
        participant_count=len(state["participant_ids"]),
        most_active_period=f"{most_active_hour:02d}:00-{(most_active_hour + 1) % 24:02d}:00",
        golden_quotes=golden_quotes,
        emoji_count=emoji_statistics.total_emoji_count,
        emoji_statistics=emoji_statistics,
        activity_visualization=ActivityVisualization(hourly_activity=hourly),
        token_usage=TokenUsage(**tu),
        chat_quality_review=quality_review,
    )
    topics = [SummaryTopic.from_dict(t) for t in state["topics"]]
    nickname_map = {uid: (us.get("nickname") or uid) for uid, us in state["user_stats"].items()}
    result = {
        "statistics": stats,
        "topics": topics,
        "user_titles": user_titles,
        "user_analysis": state["user_stats"],
        "chat_quality_review": quality_review,
        "report_template": template_name,
    }
    return result, nickname_map
