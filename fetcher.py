"""OneBot v11 群历史消息拉取与群信息工具（移植自 astrbot onebot_adapter.py）"""

import asyncio
from datetime import datetime, timedelta
from typing import List, Optional

from nekro_agent.adapters.onebot_v11.core.bot import get_bot
from nekro_agent.api.core import logger

CHUNK_SIZE = 100

USER_AVATAR_TEMPLATE = "https://q1.qlogo.cn/g?b=qq&nk={user_id}&s={size}"


def get_user_avatar_url(user_id: str, size: int = 100) -> str:
    return USER_AVATAR_TEMPLATE.format(user_id=user_id, size=size)


async def fetch_group_messages(
    group_id: str,
    days: float = 1,
    max_count: int = 1000,
    since_ts: Optional[float] = None,
) -> List[dict]:
    """分页拉取群历史消息（NapCat get_group_msg_history）

    Returns:
        OneBot 原始消息 dict 列表（按时间升序），每条形如
        {message_id, time, sender:{user_id, nickname, card}, message:[segments]}
    """
    bot = get_bot()
    if since_ts is not None:
        start_timestamp = int(since_ts)
    else:
        start_timestamp = int((datetime.now() - timedelta(days=days)).timestamp())
    now_ts = int(datetime.now().timestamp())

    all_raw: List[dict] = []
    seen_ids = set()
    current_anchor = None

    while len(all_raw) < max_count:
        fetch_count = min(CHUNK_SIZE, max_count - len(all_raw))
        params = {
            "group_id": int(group_id),
            "count": fetch_count,
            "reverseOrder": True,
        }
        if current_anchor is not None:
            params["message_seq"] = current_anchor
        try:
            result = await bot.call_api("get_group_msg_history", **params)
        except Exception as e:
            logger.warning(f"[group_analysis] get_group_msg_history 失败 group={group_id}: {e!r}")
            break

        messages = (result or {}).get("messages") or []
        if not messages:
            break

        # 动态检测本批消息顺序，找最旧一条作为下一页锚点
        first_msg, last_msg = messages[0], messages[-1]
        if first_msg.get("time", 0) <= last_msg.get("time", 0):
            chunk_earliest = first_msg
        else:
            chunk_earliest = last_msg
        chunk_earliest_time = chunk_earliest.get("time", 0)

        for raw_msg in messages:
            msg_id = str(raw_msg.get("message_id", ""))
            if msg_id and msg_id in seen_ids:
                continue
            seen_ids.add(msg_id)
            msg_time = raw_msg.get("time", 0)
            if start_timestamp <= msg_time <= now_ts + 60:
                all_raw.append(raw_msg)

        # 终止条件
        if chunk_earliest_time <= start_timestamp:
            break
        seq_val = (
            chunk_earliest.get("message_seq")
            or chunk_earliest.get("real_id")
            or chunk_earliest.get("seq")
        )
        new_anchor = seq_val if seq_val is not None else chunk_earliest.get("message_id")
        if new_anchor is None:
            break
        if current_anchor is not None and str(new_anchor) == str(current_anchor):
            break  # 已到历史尽头
        current_anchor = new_anchor
        await asyncio.sleep(0.05)

    all_raw.sort(key=lambda m: m.get("time", 0))
    logger.info(
        f"[group_analysis] 群 {group_id} 拉取到 {len(all_raw)} 条消息 "
        f"(窗口起点 {datetime.fromtimestamp(start_timestamp).strftime('%m-%d %H:%M')})",
    )
    return all_raw


async def get_group_name(group_id: str) -> str:
    try:
        info = await get_bot().call_api("get_group_info", group_id=int(group_id))
        return str(info.get("group_name") or group_id)
    except Exception:
        return str(group_id)


async def get_group_member_role(group_id: str, user_id: str) -> str:
    try:
        info = await get_bot().call_api(
            "get_group_member_info", group_id=int(group_id), user_id=int(user_id), no_cache=True,
        )
        return str(info.get("role") or "member")
    except Exception:
        return "member"


async def get_bot_self_id() -> str:
    try:
        return str(get_bot().self_id)
    except Exception:
        return ""


async def get_group_list() -> List[str]:
    """获取机器人加入的所有群号"""
    try:
        groups = await get_bot().call_api("get_group_list")
        return [str(g.get("group_id")) for g in groups or [] if g.get("group_id")]
    except Exception as e:
        logger.warning(f"[group_analysis] get_group_list 失败: {e!r}")
        return []
