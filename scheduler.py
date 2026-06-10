"""定时调度：每日定时报告 + 增量周期分析（asyncio 循环实现，无 APScheduler）"""

import asyncio
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from nekro_agent.api.core import logger

from .fetcher import get_group_list
from .plugin import get_config
from .service import in_filtered_list, is_group_allowed, perform_auto_analysis_for_group
from .service import execute_incremental_analysis

_scheduler_task: Optional[asyncio.Task] = None
# 已触发的 (date, HH:MM) 定时点
_fired_slots: set = set()
# 每群增量状态: {group_id: {"last_run": ts, "daily_count": int, "day": "YYYY-MM-DD"}}
_incr_state: Dict[str, dict] = {}


def _normalize_times(times: List[str]) -> List[str]:
    result = []
    for t in times or []:
        t = str(t).replace("：", ":").strip()
        if ":" in t:
            try:
                h, m = t.split(":")
                result.append(f"{int(h):02d}:{int(m):02d}")
            except Exception:
                continue
    return result


async def _get_scheduled_targets() -> List[Tuple[str, str]]:
    """返回 [(group_id, mode)]，mode 为 traditional / incremental"""
    cfg = get_config()
    sched_mode = cfg.SCHEDULED_GROUP_LIST_MODE
    sched_list = [str(x) for x in cfg.SCHEDULED_GROUP_LIST]
    incr_mode = cfg.INCREMENTAL_GROUP_LIST_MODE
    incr_list = [str(x) for x in cfg.INCREMENTAL_GROUP_LIST]

    # whitelist 空列表 = 不注册任何定时任务
    if sched_mode == "whitelist" and not sched_list:
        return []

    if sched_mode == "whitelist":
        candidates = sched_list
    else:
        candidates = await get_group_list()

    targets = []
    for gid in candidates:
        if not await is_group_allowed(gid):
            continue
        if not in_filtered_list(gid, sched_mode, sched_list):
            continue
        mode = "incremental" if (incr_list or incr_mode == "blacklist") and in_filtered_list(
            gid, incr_mode, incr_list,
        ) else "traditional"
        targets.append((gid, mode))
    return targets


async def _get_incremental_targets() -> List[str]:
    cfg = get_config()
    incr_mode = cfg.INCREMENTAL_GROUP_LIST_MODE
    incr_list = [str(x) for x in cfg.INCREMENTAL_GROUP_LIST]
    if incr_mode == "whitelist":
        if not incr_list:
            return []
        candidates = incr_list
    else:
        candidates = await get_group_list()
    result = []
    for gid in candidates:
        if await is_group_allowed(gid) and in_filtered_list(gid, incr_mode, incr_list):
            result.append(gid)
    return result


async def _run_scheduled_reports() -> None:
    cfg = get_config()
    targets = await _get_scheduled_targets()
    if not targets:
        return
    logger.info(f"[group_analysis] 定时分析触发，目标群: {targets}")
    sem = asyncio.Semaphore(cfg.MAX_CONCURRENT_GROUPS)

    async def run_one(idx: int, gid: str, mode: str):
        if idx > 0 and cfg.STAGGER_SECONDS > 0:
            await asyncio.sleep(cfg.STAGGER_SECONDS * idx)
        async with sem:
            await perform_auto_analysis_for_group(gid, mode)

    await asyncio.gather(*(run_one(i, gid, mode) for i, (gid, mode) in enumerate(targets)))


async def _run_incremental_ticks() -> None:
    cfg = get_config()
    now = datetime.now()
    if not (cfg.INCREMENTAL_ACTIVE_START_HOUR <= now.hour <= cfg.INCREMENTAL_ACTIVE_END_HOUR):
        return
    targets = await _get_incremental_targets()
    if not targets:
        return
    today = now.strftime("%Y-%m-%d")
    due: List[str] = []
    for gid in targets:
        st = _incr_state.setdefault(gid, {"last_run": 0.0, "daily_count": 0, "day": today})
        if st["day"] != today:
            st["day"] = today
            st["daily_count"] = 0
        if st["daily_count"] >= cfg.INCREMENTAL_MAX_DAILY:
            continue
        if now.timestamp() - st["last_run"] >= cfg.INCREMENTAL_INTERVAL_MINUTES * 60:
            due.append(gid)
    if not due:
        return

    async def run_one(idx: int, gid: str):
        if idx > 0 and cfg.INCREMENTAL_STAGGER_SECONDS > 0:
            await asyncio.sleep(cfg.INCREMENTAL_STAGGER_SECONDS * idx)
        st = _incr_state[gid]
        st["last_run"] = datetime.now().timestamp()
        try:
            result = await execute_incremental_analysis(gid)
            if result.get("success"):
                st["daily_count"] += 1
        except Exception as e:
            logger.exception(f"[group_analysis] 群 {gid} 增量分析异常: {e!r}")

    logger.info(f"[group_analysis] 增量分析触发，目标群: {due}")
    await asyncio.gather(*(run_one(i, gid) for i, gid in enumerate(due)))


async def _scheduler_loop() -> None:
    logger.info("[group_analysis] 定时调度器已启动")
    while True:
        try:
            await asyncio.sleep(20)
            cfg = get_config()
            now = datetime.now()
            hhmm = now.strftime("%H:%M")
            slot = (now.strftime("%Y-%m-%d"), hhmm)
            if hhmm in _normalize_times(cfg.AUTO_ANALYSIS_TIMES) and slot not in _fired_slots:
                _fired_slots.add(slot)
                # 防止集合无限增长
                if len(_fired_slots) > 200:
                    _fired_slots.clear()
                    _fired_slots.add(slot)
                asyncio.create_task(_run_scheduled_reports())
            await _run_incremental_ticks()
        except asyncio.CancelledError:
            logger.info("[group_analysis] 定时调度器已停止")
            return
        except Exception as e:
            logger.exception(f"[group_analysis] 调度器循环异常: {e!r}")
            await asyncio.sleep(10)


def start_scheduler() -> None:
    global _scheduler_task
    if _scheduler_task is not None and not _scheduler_task.done():
        return
    _scheduler_task = asyncio.get_event_loop().create_task(_scheduler_loop())
