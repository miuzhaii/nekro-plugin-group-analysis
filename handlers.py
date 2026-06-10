"""命令处理：/群分析 /分析设置 /设置格式 /设置模板 /查看模板 /增量状态"""

import asyncio
import time
from datetime import datetime
from typing import List, Optional

from nekro_agent.adapters.onebot_v11.matchers.command import finish_with
from nekro_agent.api.core import logger
from nonebot import get_driver, on_command
from nonebot.adapters import Bot
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageEvent
from nonebot.matcher import Matcher
from nonebot.params import CommandArg

from . import incremental, scheduler, service
from .plugin import get_config, plugin
from .render import list_available_templates
from .scheduler import start_scheduler
from .service import DuplicateGroupTaskError

OUTPUT_FORMATS = ["image", "text", "html"]
FORMAT_DISPLAY = {
    "image": "图片格式 (默认)",
    "text": "文本格式",
    "html": "HTML 文件 (发送到群文件)",
}


# ============ 启动调度器 ============

try:
    driver = get_driver()

    @driver.on_startup
    async def _start_group_analysis_scheduler():
        start_scheduler()

except ValueError:
    # NoneBot 未初始化（如独立脚本导入插件模块时），跳过启动钩子
    driver = None


# ============ 权限 ============

async def _is_admin(bot: Bot, event: MessageEvent) -> bool:
    cfg = get_config()
    user_id = str(getattr(event, "user_id", "")).strip()
    admins = {str(x).strip() for x in (cfg.MANAGE_SUPER_ADMINS or []) if str(x).strip()}
    if user_id in admins:
        return True
    if not cfg.ALLOW_GROUP_ADMIN_MANAGE:
        return False
    group_id = getattr(event, "group_id", None)
    if not group_id:
        return False
    try:
        info = await bot.get_group_member_info(group_id=int(group_id), user_id=int(user_id), no_cache=True)
        return str(info.get("role") or "").lower() in {"owner", "admin"}
    except Exception:
        return False


def _extract_text(message: Message) -> str:
    return "".join(seg.data.get("text", "") for seg in message if seg.type == "text").strip()


# ============ /群分析 ============

@on_command("群分析", aliases={"group_analysis"}, priority=5, block=True).handle()
async def handle_group_analysis(matcher: Matcher, event: GroupMessageEvent, bot: Bot, arg: Message = CommandArg()):
    if not await _is_admin(bot, event):
        await finish_with(matcher, message="❌ 仅插件管理员或群管理员可使用此命令")
    group_id = str(event.group_id)
    if not await service.is_group_allowed(group_id):
        await finish_with(matcher, message="❌ 此群未启用日常分析功能（/分析设置 enable 启用）")

    days: Optional[int] = None
    text = _extract_text(arg)
    if text:
        try:
            days = max(1, min(7, int(text.split()[0])))
        except ValueError:
            pass

    async def _run():
        try:
            result = await service.execute_daily_analysis(group_id, manual=True, days=days)
            if not result.get("success"):
                if result.get("reason") == "no_messages":
                    await service._send_group_text(group_id, "❌ 未找到足够的群聊记录")
                else:
                    await service._send_group_text(group_id, "❌ 分析失败，原因未知")
                return
            await service.send_analysis_report(group_id, result)
        except DuplicateGroupTaskError:
            await service._send_group_text(group_id, "📊 该群的分析任务正在执行中，请稍后再试哦~")
        except Exception as e:
            logger.exception(f"[group_analysis] 群分析失败: {e!r}")
            await service._send_group_text(
                group_id, f"❌ 分析失败: {str(e)[:150]}。请检查网络连接和 LLM 配置，或联系管理员",
            )

    asyncio.create_task(_run())
    await finish_with(
        matcher,
        message=f"🔍 正在启动分析引擎，拉取最近 {days or get_config().ANALYSIS_DAYS} 天消息并分析，请稍候（约 1-3 分钟）...",
    )


# ============ /分析设置 ============

@on_command("分析设置", aliases={"analysis_settings"}, priority=5, block=True).handle()
async def handle_analysis_settings(matcher: Matcher, event: GroupMessageEvent, bot: Bot, arg: Message = CommandArg()):
    if not await _is_admin(bot, event):
        await finish_with(matcher, message="❌ 仅插件管理员或群管理员可使用此命令")
    cfg = get_config()
    group_id = str(event.group_id)
    action = (_extract_text(arg) or "status").strip().lower()

    if action == "enable":
        mode = cfg.GROUP_LIST_MODE
        glist = await service.get_effective_group_list()
        if mode == "whitelist":
            if group_id in glist:
                await finish_with(matcher, message="ℹ️ 当前群已在白名单中")
            glist.append(group_id)
            await service.set_runtime_override("group_list", glist)
            await finish_with(matcher, message=f"✅ 已将当前群加入白名单\nID: {group_id}")
        elif mode == "blacklist":
            if group_id not in glist:
                await finish_with(matcher, message="ℹ️ 当前群不在黑名单中")
            glist.remove(group_id)
            await service.set_runtime_override("group_list", glist)
            await finish_with(matcher, message="✅ 已将当前群从黑名单移除")
        else:
            await finish_with(matcher, message="ℹ️ 当前为无限制模式，所有群聊默认启用")

    elif action == "disable":
        mode = cfg.GROUP_LIST_MODE
        glist = await service.get_effective_group_list()
        if mode == "whitelist":
            if group_id not in glist:
                await finish_with(matcher, message="ℹ️ 当前群不在白名单中")
            glist.remove(group_id)
            await service.set_runtime_override("group_list", glist)
            await finish_with(matcher, message="✅ 已将当前群从白名单移除")
        elif mode == "blacklist":
            if group_id in glist:
                await finish_with(matcher, message="ℹ️ 当前群已在黑名单中")
            glist.append(group_id)
            await service.set_runtime_override("group_list", glist)
            await finish_with(matcher, message=f"✅ 已将当前群加入黑名单\nID: {group_id}")
        else:
            await finish_with(matcher, message="ℹ️ 当前为无限制模式，如需禁用请在插件配置中切换到黑名单模式")

    elif action == "reload":
        start_scheduler()
        await finish_with(matcher, message="✅ 配置即时生效，调度器运行中")

    elif action == "test":
        if not await service.is_group_allowed(group_id):
            await finish_with(matcher, message="❌ 请先启用当前群的分析功能")

        async def _test():
            try:
                incr_targets = await scheduler._get_incremental_targets()
                mode = "incremental" if group_id in incr_targets else "traditional"
                await service.perform_auto_analysis_for_group(group_id, mode)
                await service._send_group_text(group_id, "✅ 自动分析测试完成")
            except Exception as e:
                await service._send_group_text(group_id, f"❌ 自动分析测试失败: {str(e)[:150]}")

        asyncio.create_task(_test())
        await finish_with(matcher, message="🧪 开始测试自动分析功能...")

    elif action == "incremental_debug":
        overrides = await service.get_runtime_overrides()
        current = overrides.get("incremental_report_immediately")
        if current is None:
            current = cfg.INCREMENTAL_REPORT_IMMEDIATELY
        new_state = not current
        await service.set_runtime_override("incremental_report_immediately", new_state)
        await finish_with(matcher, message=f"✅ 增量分析立即报告模式: {'已启用' if new_state else '已禁用'}")

    else:  # status
        is_allowed = await service.is_group_allowed(group_id)
        mode = cfg.GROUP_LIST_MODE
        sched_targets = await scheduler._get_scheduled_targets()
        auto_on = any(g == group_id for g, _ in sched_targets)
        incr_targets = await scheduler._get_incremental_targets()
        incr_on = group_id in incr_targets
        incr_text = "未启用"
        if incr_on:
            incr_text = (
                f"已启用 (间隔{cfg.INCREMENTAL_INTERVAL_MINUTES}分钟, 最多{cfg.INCREMENTAL_MAX_DAILY}次/天, "
                f"活跃时段{cfg.INCREMENTAL_ACTIVE_START_HOUR}:00-{cfg.INCREMENTAL_ACTIVE_END_HOUR}:00)"
            )
        overrides = await service.get_runtime_overrides()
        debug_report = overrides.get("incremental_report_immediately")
        if debug_report is None:
            debug_report = cfg.INCREMENTAL_REPORT_IMMEDIATELY
        output_format = await service.get_effective_output_format()
        await finish_with(
            matcher,
            message=f"""📊 当前群分析功能状态:
• 群分析功能: {"已启用" if is_allowed else "未启用"} (模式: {mode})
• 自动分析: {"已启用" if auto_on else "未启用"} ({", ".join(cfg.AUTO_ANALYSIS_TIMES)})
• 增量分析: {incr_text}
• 调试模式: {"✅ 开启" if debug_report else "❌ 关闭"} (增量立即报告)
• 输出格式: {output_format}
• 报告模板: {await service.get_effective_template()}
• 最小消息数: {cfg.MIN_MESSAGES_THRESHOLD}

💡 可用命令: enable, disable, status, reload, test, incremental_debug
💡 其他命令: /设置格式, /设置模板, /查看模板, /增量状态""",
        )


# ============ /设置格式 ============

@on_command("设置格式", aliases={"set_format"}, priority=5, block=True).handle()
async def handle_set_format(matcher: Matcher, event: MessageEvent, bot: Bot, arg: Message = CommandArg()):
    if not await _is_admin(bot, event):
        await finish_with(matcher, message="❌ 仅插件管理员或群管理员可使用此命令")
    text = _extract_text(arg).strip().lower()
    if not text:
        current = await service.get_effective_output_format()
        fmt_list = "\n".join(f"【{i}】{f} - {FORMAT_DISPLAY[f]}" for i, f in enumerate(OUTPUT_FORMATS, 1))
        await finish_with(
            matcher,
            message=f"📊 当前输出格式: {current}\n\n可用格式:\n{fmt_list}\n\n用法: /设置格式 [名称或序号]",
        )
    target = None
    if text.isdigit() and 1 <= int(text) <= len(OUTPUT_FORMATS):
        target = OUTPUT_FORMATS[int(text) - 1]
    elif text in OUTPUT_FORMATS:
        target = text
    if not target:
        await finish_with(
            matcher,
            message=f"❌ 无效的格式类型 '{text}'。可用: {', '.join(OUTPUT_FORMATS)} 或序号 1-{len(OUTPUT_FORMATS)}",
        )
    await service.set_runtime_override("output_format", target)
    await finish_with(matcher, message=f"✅ 输出格式已设置为: {target}")


# ============ /设置模板 与 /查看模板 ============

@on_command("设置模板", aliases={"set_template"}, priority=5, block=True).handle()
async def handle_set_template(matcher: Matcher, event: MessageEvent, bot: Bot, arg: Message = CommandArg()):
    if not await _is_admin(bot, event):
        await finish_with(matcher, message="❌ 仅插件管理员或群管理员可使用此命令")
    templates = list_available_templates()
    text = _extract_text(arg).strip()
    if not text:
        current = await service.get_effective_template()
        tpl_list = "\n".join(f"【{i}】{t}" for i, t in enumerate(templates, 1))
        await finish_with(
            matcher,
            message=f"🎨 当前报告模板: {current}\n\n可用模板:\n{tpl_list}\n\n用法: /设置模板 [模板名称或序号]",
        )
    target = None
    if text.isdigit() and 1 <= int(text) <= len(templates):
        target = templates[int(text) - 1]
    else:
        for t in templates:
            if t.lower() == text.lower():
                target = t
                break
    if not target:
        await finish_with(matcher, message=f"❌ 模板 '{text}' 不存在，可用: {', '.join(templates)}")
    await service.set_runtime_override("report_template", target)
    await finish_with(matcher, message=f"✅ 报告模板已设置为: {target}")


@on_command("查看模板", aliases={"view_templates"}, priority=5, block=True).handle()
async def handle_view_templates(matcher: Matcher, event: MessageEvent, bot: Bot, arg: Message = CommandArg()):
    if not await _is_admin(bot, event):
        await finish_with(matcher, message="❌ 仅插件管理员或群管理员可使用此命令")
    templates = list_available_templates()
    if not templates:
        await finish_with(matcher, message="❌ 未找到任何可用的报告模板")
    current = await service.get_effective_template()
    lines = [f"{'▶' if t == current else '　'}【{i}】{t}" for i, t in enumerate(templates, 1)]
    await finish_with(
        matcher,
        message="🎨 可用报告模板\n━━━━━━━━━━━━━━━\n" + "\n".join(lines)
        + "\n━━━━━━━━━━━━━━━\n用法: /设置模板 [名称或序号]",
    )


# ============ /增量状态 ============

@on_command("增量状态", aliases={"incremental_status"}, priority=5, block=True).handle()
async def handle_incremental_status(matcher: Matcher, event: GroupMessageEvent, bot: Bot, arg: Message = CommandArg()):
    if not await _is_admin(bot, event):
        await finish_with(matcher, message="❌ 仅插件管理员或群管理员可使用此命令")
    cfg = get_config()
    group_id = str(event.group_id)
    incr_targets = await scheduler._get_incremental_targets()
    if group_id not in incr_targets:
        await finish_with(matcher, message="ℹ️ 当前群未启用增量分析模式，请在插件配置中开启")

    window_end = time.time()
    window_start = window_end - cfg.ANALYSIS_DAYS * 24 * 3600
    batches = await incremental.query_batches(group_id, window_start, window_end)
    start_str = datetime.fromtimestamp(window_start).strftime("%m-%d %H:%M")
    end_str = datetime.fromtimestamp(window_end).strftime("%m-%d %H:%M")
    if not batches:
        await finish_with(matcher, message=f"📊 滑动窗口 ({start_str} ~ {end_str}) 内尚无增量分析数据")
    state = incremental.merge_batches(batches)
    summary = incremental.get_state_summary(state, f"{start_str} ~ {end_str}")
    await finish_with(
        matcher,
        message=(
            f"📊 增量分析状态 (窗口: {summary['window']})\n"
            f"• 分析次数: {summary['total_analyses']}\n"
            f"• 累计消息: {summary['total_messages']}\n"
            f"• 话题数: {summary['topics_count']}\n"
            f"• 金句数: {summary['quotes_count']}\n"
            f"• 参与者: {summary['participants']}\n"
            f"• 高峰时段: {summary['peak_hours']}"
        ),
    )
