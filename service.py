"""分析编排与报告发送（移植自 astrbot analysis_application_service / dispatcher）"""

import asyncio
import base64
import json
import os
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from nekro_agent.adapters.onebot_v11.core.bot import get_bot
from nekro_agent.api.core import logger

from . import incremental
from .fetcher import fetch_group_messages, get_bot_self_id, get_group_name
from .llm import (
    analyze_chat_quality,
    analyze_golden_quotes,
    analyze_topics,
    analyze_user_titles,
    summarize_quality_reviews,
)
from .models import QualityReview, TokenUsage, UserTitle
from .plugin import get_config, plugin, store
from .render import generate_html_report, generate_image_report, generate_text_report
from .statistics import (
    analyze_user_activity,
    build_llm_text_messages,
    calculate_group_statistics,
    clean_messages,
    get_top_users,
)

RUNTIME_KEY = "runtime_overrides"

# 正在分析中的群（防重复任务）
_active_tasks: Set[str] = set()


class DuplicateGroupTaskError(Exception):
    pass


# ============ 运行时可变设置（命令修改，优先于配置） ============

async def get_runtime_overrides() -> dict:
    raw = await store.get(chat_key="global", store_key=RUNTIME_KEY)
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


async def set_runtime_override(key: str, value) -> None:
    data = await get_runtime_overrides()
    data[key] = value
    await store.set(chat_key="global", store_key=RUNTIME_KEY, value=json.dumps(data, ensure_ascii=False))


async def get_effective_output_format() -> str:
    data = await get_runtime_overrides()
    return data.get("output_format") or get_config().OUTPUT_FORMAT


async def get_effective_template() -> str:
    data = await get_runtime_overrides()
    return data.get("report_template") or get_config().REPORT_TEMPLATE


async def get_effective_group_list() -> List[str]:
    data = await get_runtime_overrides()
    if "group_list" in data and isinstance(data["group_list"], list):
        return [str(x) for x in data["group_list"]]
    return [str(x) for x in get_config().GROUP_LIST]


async def is_group_allowed(group_id: str) -> bool:
    cfg = get_config()
    mode = cfg.GROUP_LIST_MODE
    if mode == "none":
        return True
    glist = await get_effective_group_list()
    in_list = str(group_id) in glist
    if mode == "whitelist":
        return in_list
    if mode == "blacklist":
        return not in_list
    return True


def in_filtered_list(group_id: str, mode: str, glist: List[str]) -> bool:
    """定时/增量名单判断：whitelist=必须在列表，blacklist=必须不在列表"""
    in_list = str(group_id) in [str(x) for x in glist]
    if mode == "whitelist":
        return in_list
    if mode == "blacklist":
        return not in_list
    return False


async def get_effective_bot_ids() -> List[str]:
    cfg = get_config()
    ids = [str(x) for x in cfg.BOT_SELF_IDS]
    self_id = await get_bot_self_id()
    if self_id and self_id not in ids:
        ids.append(self_id)
    return ids


# ============ 全量分析 ============

async def execute_daily_analysis(group_id: str, manual: bool = True, days: Optional[int] = None) -> dict:
    cfg = get_config()
    lock_key = f"{group_id}:daily"
    if lock_key in _active_tasks:
        raise DuplicateGroupTaskError
    _active_tasks.add(lock_key)
    try:
        bot_ids = await get_effective_bot_ids()
        raw_messages = await fetch_group_messages(
            group_id, days=days or cfg.ANALYSIS_DAYS, max_count=cfg.MAX_MESSAGES,
        )
        messages = clean_messages(raw_messages, bot_ids)
        if not messages:
            return {"success": False, "reason": "no_messages"}
        if not manual and len(messages) < cfg.MIN_MESSAGES_THRESHOLD:
            logger.info(
                f"[group_analysis] 群 {group_id} 消息数 {len(messages)} 低于阈值 "
                f"{cfg.MIN_MESSAGES_THRESHOLD}，跳过自动分析",
            )
            return {"success": False, "reason": "below_threshold"}

        stats = calculate_group_statistics(messages)
        user_activity = analyze_user_activity(messages, bot_ids)
        top_users = get_top_users(user_activity)
        text_messages = build_llm_text_messages(messages, bot_ids)

        # 四个 LLM 分析器并发执行（llm.py 内部有信号量限流）
        topics_task = analyze_topics(text_messages)
        titles_task = analyze_user_titles(user_activity, top_users, bot_ids)
        quotes_task = analyze_golden_quotes(text_messages)
        quality_task = analyze_chat_quality(text_messages)
        (topics, t_usage), (titles, u_usage), (quotes, q_usage), (quality, c_usage) = await asyncio.gather(
            topics_task, titles_task, quotes_task, quality_task,
        )

        total_usage = TokenUsage()
        for u in (t_usage, u_usage, q_usage, c_usage):
            total_usage.add(u)
        stats.golden_quotes = quotes
        stats.token_usage = total_usage
        stats.chat_quality_review = quality

        nickname_map = {uid: (s.get("nickname") or uid) for uid, s in user_activity.items()}
        analysis_result = {
            "statistics": stats,
            "topics": topics,
            "user_titles": titles,
            "user_analysis": user_activity,
            "chat_quality_review": quality,
            "report_template": await get_effective_template(),
        }
        return {
            "success": True,
            "analysis_result": analysis_result,
            "nickname_map": nickname_map,
            "messages_count": len(messages),
            "group_id": group_id,
        }
    finally:
        _active_tasks.discard(lock_key)


# ============ 报告发送 ============

def _napcat_visible_path(path: Path) -> str:
    """nekro 共享数据目录映射为 NapCat 容器内可见路径"""
    text = str(path.resolve())
    candidate_roots = [(os.getenv("NEKRO_DATA_DIR") or "").rstrip("/"), "/root/srv/nekro_agent"]
    try:
        candidate_roots.append(str(Path(str(plugin.get_plugin_data_dir())).parents[1]))
    except Exception:
        pass
    for host_root in candidate_roots:
        if host_root and text.startswith(host_root):
            return "/app/nekro_agent_data" + text[len(host_root):]
    return text


async def _send_group_text(group_id: str, text: str) -> None:
    await get_bot().call_api(
        "send_msg", message_type="group", group_id=int(group_id),
        message=[{"type": "text", "data": {"text": text}}],
    )


def _is_image_send_ack_timeout(exc: Exception) -> bool:
    text = repr(exc)
    return "retcode=1200" in text and "invoke timeout" in text


async def _send_group_image(group_id: str, image_bytes: bytes) -> None:
    b64 = base64.b64encode(image_bytes).decode()
    try:
        await get_bot().call_api(
            "send_msg", message_type="group", group_id=int(group_id),
            message=[{"type": "image", "data": {"file": f"base64://{b64}"}}],
        )
    except Exception as e:
        if _is_image_send_ack_timeout(e):
            logger.warning(f"[group_analysis] 图片发送 ACK 超时，可能已成功送达 group={group_id}: {e!r}")
            return
        raise



async def _upload_group_file(group_id: str, file_path: Path, filename: str) -> bool:
    try:
        cfg = get_config()
        kwargs = {
            "group_id": int(group_id),
            "file": _napcat_visible_path(file_path),
            "name": filename,
        }
        if cfg.GROUP_FILE_FOLDER:
            try:
                root = await get_bot().call_api("get_group_root_files", group_id=int(group_id))
                for folder in (root or {}).get("folders") or []:
                    if folder.get("folder_name") == cfg.GROUP_FILE_FOLDER:
                        kwargs["folder"] = folder.get("folder_id")
                        break
            except Exception:
                pass
        await get_bot().call_api("upload_group_file", **kwargs)
        return True
    except Exception as e:
        logger.warning(f"[group_analysis] 群文件上传失败 group={group_id}: {e!r}")
        return False


def _shared_report_dir() -> Path:
    shared_root = Path(os.getenv("NEKRO_DATA_DIR") or "/root/srv/nekro_agent")
    d = shared_root / "group_analysis_reports"
    d.mkdir(parents=True, exist_ok=True)
    return d


async def send_analysis_report(group_id: str, result: dict) -> str:
    """渲染并发送报告，返回实际使用的格式"""
    cfg = get_config()
    analysis_result = result["analysis_result"]
    nickname_map = result.get("nickname_map") or {}
    output_format = await get_effective_output_format()

    if output_format == "image":
        image_data, _html = await generate_image_report(analysis_result, nickname_map)
        if image_data:
            await _send_group_image(group_id, image_data)
            if cfg.ENABLE_GROUP_FILE_UPLOAD:
                try:
                    group_name = await get_group_name(group_id)
                    safe_name = "".join(c for c in group_name if c not in '\\/:*?"<>|').strip() or group_id
                    ext = ".png" if image_data.startswith(b"\x89PNG") else ".jpg"
                    fname = f"群分析报告_{safe_name}_{datetime.now().strftime('%Y-%m-%d_%H%M')}{ext}"
                    fpath = _shared_report_dir() / f"{uuid.uuid4().hex}{ext}"
                    fpath.write_bytes(image_data)
                    await _upload_group_file(group_id, fpath, fname)
                except Exception as e:
                    logger.warning(f"[group_analysis] 上传群文件异常: {e!r}")
            return "image"
        logger.warning(f"[group_analysis] 群 {group_id} 图片报告生成失败，回退文本")
        await _send_group_text(group_id, generate_text_report(analysis_result, nickname_map))
        return "text(fallback)"

    if output_format == "html":
        html_path = await generate_html_report(analysis_result, nickname_map, group_id)
        if html_path:
            # 复制到共享目录供 NapCat 访问
            shared_path = _shared_report_dir() / html_path.name
            shared_path.write_bytes(html_path.read_bytes())
            sent = await _upload_group_file(group_id, shared_path, html_path.name)
            if sent:
                await _send_group_text(group_id, f"📊 群聊分析报告已上传到群文件：{html_path.name}")
                return "html"
        logger.warning(f"[group_analysis] 群 {group_id} HTML 报告发送失败，回退文本")
        await _send_group_text(group_id, generate_text_report(analysis_result, nickname_map))
        return "text(fallback)"

    await _send_group_text(group_id, generate_text_report(analysis_result, nickname_map))
    return "text"


# ============ 增量分析 ============

async def execute_incremental_analysis(group_id: str) -> dict:
    """单批次增量提取：拉取自上次以来的新消息 → 小批量分析 → 存储批次"""
    cfg = get_config()
    lock_key = f"{group_id}:incr"
    if lock_key in _active_tasks:
        return {"success": False, "reason": "already_running"}
    _active_tasks.add(lock_key)
    try:
        bot_ids = await get_effective_bot_ids()
        last_ts = await incremental.get_last_analyzed_ts(group_id)
        window_floor = time.time() - cfg.ANALYSIS_DAYS * 24 * 3600
        since_ts = max(last_ts or window_floor, window_floor)
        raw_messages = await fetch_group_messages(
            group_id, max_count=cfg.INCREMENTAL_SAFE_LIMIT, since_ts=since_ts + 1,
        )
        messages = clean_messages(raw_messages, bot_ids)
        if len(messages) < cfg.INCREMENTAL_MIN_MESSAGES:
            logger.info(
                f"[group_analysis] 群 {group_id} 增量批次消息 {len(messages)} 低于阈值 "
                f"{cfg.INCREMENTAL_MIN_MESSAGES}，跳过",
            )
            return {"success": False, "reason": "below_threshold"}

        stats = calculate_group_statistics(messages)
        user_activity = analyze_user_activity(messages, bot_ids)
        text_messages = build_llm_text_messages(messages, bot_ids)

        topics_task = analyze_topics(text_messages, max_topics=cfg.INCREMENTAL_TOPICS_PER_BATCH)
        quotes_task = analyze_golden_quotes(text_messages, max_quotes=cfg.INCREMENTAL_QUOTES_PER_BATCH)
        quality_task = analyze_chat_quality(text_messages)
        (topics, t_usage), (quotes, q_usage), (quality, c_usage) = await asyncio.gather(
            topics_task, quotes_task, quality_task,
        )
        total_usage = TokenUsage()
        for u in (t_usage, q_usage, c_usage):
            total_usage.add(u)

        es = stats.emoji_statistics
        last_message_ts = max((m.get("time", 0) for m in messages), default=time.time())
        batch = {
            "group_id": str(group_id),
            "batch_id": uuid.uuid4().hex,
            "timestamp": time.time(),
            "messages_count": stats.message_count,
            "characters_count": stats.total_characters,
            "hourly_msg_counts": {str(k): v for k, v in stats.activity_visualization.hourly_activity.items()},
            "user_stats": {
                uid: {**s, "hours": {str(h): c for h, c in s["hours"].items()}}
                for uid, s in user_activity.items()
            },
            "emoji_stats": {
                "face_count": es.face_count,
                "mface_count": es.mface_count,
                "bface_count": es.bface_count,
                "sface_count": es.sface_count,
                "other_emoji_count": es.other_emoji_count,
                "face_details": es.face_details,
            },
            "topics": [t.to_dict() for t in topics],
            "golden_quotes": [q.to_dict() for q in quotes],
            "token_usage": {
                "prompt_tokens": total_usage.prompt_tokens,
                "completion_tokens": total_usage.completion_tokens,
                "total_tokens": total_usage.total_tokens,
            },
            "chat_quality_review": quality.to_dict() if quality else None,
            "last_message_timestamp": last_message_ts,
            "participant_ids": sorted({str((m.get("sender") or {}).get("user_id", "")) for m in messages}),
        }
        await incremental.save_batch(group_id, batch)
        await incremental.set_last_analyzed_ts(group_id, float(last_message_ts))
        logger.info(
            f"[group_analysis] 群 {group_id} 增量批次完成: {stats.message_count} 条消息, "
            f"{len(topics)} 话题, {len(quotes)} 金句",
        )

        overrides = await get_runtime_overrides()
        report_immediately = overrides.get("incremental_report_immediately")
        if report_immediately is None:
            report_immediately = cfg.INCREMENTAL_REPORT_IMMEDIATELY
        if report_immediately:
            await execute_incremental_final_report(group_id, send=True)
        return {"success": True, "messages_count": stats.message_count}
    finally:
        _active_tasks.discard(lock_key)


async def execute_incremental_final_report(group_id: str, send: bool = True) -> dict:
    """增量最终报告：合并滑动窗口内批次 → 称号/质量汇总 LLM → 发送"""
    cfg = get_config()
    window_end = time.time()
    window_start = window_end - cfg.ANALYSIS_DAYS * 24 * 3600
    batches = await incremental.query_batches(group_id, window_start, window_end)
    if not batches:
        return {"success": False, "reason": "no_batches"}

    state = incremental.merge_batches(batches)
    if state["total_messages"] <= 0:
        return {"success": False, "reason": "no_messages"}

    bot_ids = await get_effective_bot_ids()
    top_users = get_top_users(
        {uid: {**s, "hours": {int(k): v for k, v in s["hours"].items()}} for uid, s in state["user_stats"].items()},
    )
    titles: List[UserTitle] = []
    title_usage = TokenUsage()
    if cfg.USER_TITLE_ENABLED:
        titles, title_usage = await analyze_user_titles(state["user_stats"], top_users, bot_ids)

    quality_review: Optional[QualityReview] = None
    if state["quality_reviews"]:
        reviews = [QualityReview.from_dict(r) for r in state["quality_reviews"] if r]
        quality_review, _ = await summarize_quality_reviews(reviews)

    template_name = await get_effective_template()
    result_dict, nickname_map = incremental.build_analysis_result(state, titles, quality_review, template_name)
    result_dict["statistics"].token_usage.add(title_usage)

    result = {
        "success": True,
        "analysis_result": result_dict,
        "nickname_map": nickname_map,
        "group_id": group_id,
    }
    if send:
        await send_analysis_report(group_id, result)
    # 清理超过 2 倍窗口的旧批次
    await incremental.cleanup_old_batches(group_id, window_end - cfg.ANALYSIS_DAYS * 2 * 24 * 3600)
    return result


async def perform_auto_analysis_for_group(group_id: str, mode: str = "traditional") -> None:
    """定时任务入口：执行分析并发送报告（增量失败可回退全量）"""
    cfg = get_config()
    try:
        if mode == "incremental":
            result = await execute_incremental_final_report(group_id, send=True)
            if result.get("success"):
                return
            reason = result.get("reason", "")
            if reason in ("already_running",):
                return
            if cfg.INCREMENTAL_FALLBACK_ENABLED:
                logger.warning(f"[group_analysis] 群 {group_id} 增量报告失败({reason})，回退全量分析")
            else:
                return
        result = await execute_daily_analysis(group_id, manual=False)
        if result.get("success"):
            await send_analysis_report(group_id, result)
        else:
            logger.info(f"[group_analysis] 群 {group_id} 自动分析跳过: {result.get('reason')}")
    except DuplicateGroupTaskError:
        logger.info(f"[group_analysis] 群 {group_id} 已有分析任务在执行")
    except Exception as e:
        logger.exception(f"[group_analysis] 群 {group_id} 自动分析失败: {e!r}")
