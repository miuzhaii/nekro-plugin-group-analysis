"""报告渲染：jinja2 模板 + 头像 + HTML→图片（browserless）+ 文本报告

移植自 astrbot 插件 reporting/generators.py（渲染服务由 AstrBot T2I 换为
自托管 browserless/chromium 的 /screenshot API）。
"""

import asyncio
import base64
import html as html_mod
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import httpx
from jinja2 import Environment, FileSystemLoader, select_autoescape
from nekro_agent.api.core import logger

from .fetcher import get_bot_self_id, get_user_avatar_url
from .models import GroupStatistics, QualityReview
from .plugin import get_config, plugin
from .statistics import get_hourly_chart_data

TEMPLATES_DIR = Path(__file__).parent / "templates"
AVATAR_CACHE_TTL = 259200  # 3 天

_envs: Dict[str, Environment] = {}


def list_available_templates() -> List[str]:
    if not TEMPLATES_DIR.exists():
        return []
    return sorted(
        d.name for d in TEMPLATES_DIR.iterdir() if d.is_dir() and (d / "image_template.html").exists()
    )


def _get_env(template_name: str) -> Environment:
    tpl_dir = TEMPLATES_DIR / template_name
    if not tpl_dir.exists():
        logger.warning(f"[group_analysis] 模板 {template_name} 不存在，回退 scrapbook")
        template_name = "scrapbook"
        tpl_dir = TEMPLATES_DIR / template_name
    if template_name not in _envs:
        _envs[template_name] = Environment(
            loader=FileSystemLoader(str(tpl_dir)),
            autoescape=select_autoescape(["html", "xml"]),
            trim_blocks=True,
            lstrip_blocks=True,
        )
    return _envs[template_name]


def render_component(template_name: str, component: str, **kwargs) -> str:
    try:
        env = _get_env(template_name)
        return env.get_template(component).render(**kwargs)
    except Exception as e:
        logger.warning(f"[group_analysis] 渲染组件 {component} 失败: {e!r}")
        return ""


# ============ 头像 ============

def _avatar_cache_dir() -> Path:
    d = Path(str(plugin.get_plugin_data_dir())) / "avatar_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _detect_mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if data.startswith(b"GIF8"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


async def get_avatar_data_uri(user_id: str) -> Optional[str]:
    user_id = str(user_id).strip()
    if not user_id.isdigit():
        return None
    cache_file = _avatar_cache_dir() / f"{user_id}.bin"
    try:
        if cache_file.exists() and time.time() - cache_file.stat().st_mtime < AVATAR_CACHE_TTL:
            data = cache_file.read_bytes()
            if data:
                return f"data:{_detect_mime(data)};base64,{base64.b64encode(data).decode()}"
    except Exception:
        pass
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(get_user_avatar_url(user_id, 100), follow_redirects=True)
            resp.raise_for_status()
            data = resp.content
        if not data or len(data) < 100:
            return None
        cache_file.write_bytes(data)
        return f"data:{_detect_mime(data)};base64,{base64.b64encode(data).decode()}"
    except Exception as e:
        logger.debug(f"[group_analysis] 获取头像失败 {user_id}: {e!r}")
        return None


MENTION_PATTERN = re.compile(r"\[(\d{5,12})\]")


async def render_mentions(text: str, nickname_map: Dict[str, str]) -> str:
    """把文本中的 [user_id] 替换为头像胶囊 HTML（其余部分转义）"""
    if not text:
        return ""
    parts: List[str] = []
    last_end = 0
    for m in MENTION_PATTERN.finditer(text):
        parts.append(html_mod.escape(text[last_end : m.start()]))
        uid = m.group(1)
        name = nickname_map.get(uid, uid)
        avatar = await get_avatar_data_uri(uid)
        if avatar:
            parts.append(
                '<span class="user-capsule" style="display:inline-flex;align-items:center;gap:4px;'
                "padding:1px 8px 1px 2px;border-radius:999px;background:rgba(0,0,0,0.06);"
                'vertical-align:middle;white-space:nowrap;">'
                f'<img src="{avatar}" style="width:18px;height:18px;border-radius:50%;display:inline-block;"/>'
                f"<span>{html_mod.escape(name)}</span></span>"
            )
        else:
            parts.append(f"<strong>{html_mod.escape(name)}</strong>")
        last_end = m.end()
    parts.append(html_mod.escape(text[last_end:]))
    return "".join(parts)


def strip_mentions(text: str, nickname_map: Dict[str, str]) -> str:
    """文本报告：把 [user_id] 还原为昵称"""
    return MENTION_PATTERN.sub(lambda m: nickname_map.get(m.group(1), m.group(1)), text or "")


# ============ 渲染数据组装 ============

def _common_context() -> dict:
    cfg = get_config()
    if cfg.T2I_FONT_SOURCE == "Mainland":
        gf, gs = cfg.T2I_MAINLAND_GOOGLE_FONTS, cfg.T2I_MAINLAND_GSTATIC
    else:
        gf, gs = cfg.T2I_OVERSEAS_GOOGLE_FONTS, cfg.T2I_OVERSEAS_GSTATIC
    return {
        "t2i_font_source": cfg.T2I_FONT_SOURCE,
        "t2i_google_fonts_mirror": gf,
        "t2i_gstatic_mirror": gs,
        "t2i_atri_font_mirror": cfg.T2I_ATRI_FONT_MIRROR,
        "profile_image_opacity": cfg.PROFILE_IMAGE_OPACITY,
        "profile_image_size_mode": cfg.PROFILE_IMAGE_SIZE_MODE,
    }


async def prepare_render_data(analysis_result: dict, nickname_map: Dict[str, str]) -> dict:
    cfg = get_config()
    stats: GroupStatistics = analysis_result["statistics"]
    topics = analysis_result.get("topics") or []
    user_titles = analysis_result.get("user_titles") or []
    golden_quotes = stats.golden_quotes or []
    quality: Optional[QualityReview] = analysis_result.get("chat_quality_review")
    template_name = analysis_result.get("report_template") or cfg.REPORT_TEMPLATE
    common = _common_context()

    # 话题
    topics_list = []
    for i, topic in enumerate(topics[: cfg.MAX_TOPICS], 1):
        detail_html = await render_mentions(topic.detail, nickname_map)
        topics_list.append(
            {
                "index": i,
                "topic": topic,
                "contributors": "、".join(topic.contributors),
                "detail": detail_html,
            },
        )
    topics_html = render_component(template_name, "topic_item.html", topics=topics_list, **common)

    # 用户称号
    titles_list = []
    for t in user_titles[: cfg.MAX_USER_TITLES]:
        avatar = await get_avatar_data_uri(t.user_id)
        titles_list.append(
            {
                "name": t.name,
                "title": t.title,
                "mbti": t.mbti,
                "reason": t.reason,
                "avatar_data": avatar or "",
                "profile_image": "",
                "profile_display": t.mbti,
            },
        )
    titles_html = render_component(template_name, "user_title_item.html", titles=titles_list, **common)

    # 金句
    quotes_list = []
    for q in golden_quotes[: cfg.MAX_GOLDEN_QUOTES]:
        avatar = await get_avatar_data_uri(q.user_id) if q.user_id else None
        reason_html = await render_mentions(q.reason, nickname_map)
        quotes_list.append(
            {
                "content": q.content,
                "sender": q.sender,
                "reason": reason_html,
                "avatar_url": avatar or "",
            },
        )
    quotes_html = render_component(template_name, "quote_item.html", quotes=quotes_list, **common)

    # 质量锐评（气泡旁的角色头像自动使用 bot 自己的 QQ 头像，失败回退模板内置图）
    bot_avatar = ""
    try:
        self_id = await get_bot_self_id()
        if self_id:
            bot_avatar = await get_avatar_data_uri(self_id) or ""
    except Exception:
        bot_avatar = ""
    chat_quality_html = ""
    if quality and quality.dimensions:
        chat_quality_html = render_component(
            template_name,
            "chat_quality_item.html",
            bot_avatar=bot_avatar,
            title=quality.title,
            subtitle=quality.subtitle,
            dimensions=[
                {"name": d.name, "percentage": d.percentage, "comment": d.comment, "color": d.color}
                for d in quality.dimensions
            ],
            summary=quality.summary,
            **common,
        )

    # 活跃图表
    chart_data = get_hourly_chart_data(stats.activity_visualization.hourly_activity)
    hourly_chart_html = render_component(template_name, "activity_chart.html", chart_data=chart_data, **common)

    now = datetime.now()
    return {
        "message_count": stats.message_count,
        "participant_count": stats.participant_count,
        "total_characters": stats.total_characters,
        "emoji_count": stats.emoji_count,
        "most_active_period": stats.most_active_period,
        "current_date": now.strftime("%Y年%m月%d日"),
        "current_datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
        "hourly_chart_html": hourly_chart_html,
        "topics_html": topics_html,
        "titles_html": titles_html,
        "quotes_html": quotes_html,
        "chat_quality_html": chat_quality_html,
        "total_tokens": stats.token_usage.total_tokens,
        "prompt_tokens": stats.token_usage.prompt_tokens,
        "completion_tokens": stats.token_usage.completion_tokens,
        "avatar_reuse_registry": {},
        "avatar_reuse_aliases": {},
        "bot_avatar": bot_avatar,
        **common,
    }


# ============ HTML → 图片（browserless /screenshot） ============

async def html_to_image(html_content: str) -> Optional[bytes]:
    """两轮渲染策略：r1 PNG 高清，r2 JPEG 降级"""
    cfg = get_config()
    endpoint = cfg.RENDER_ENDPOINT.rstrip("/")
    url = f"{endpoint}/screenshot"
    if cfg.RENDER_TOKEN:
        url += f"?token={cfg.RENDER_TOKEN}"
    strategies = [
        {
            "type": "png",
            "scale": 2,
            "timeout": cfg.RENDER_TIMEOUT_R1,
        },
        {
            "type": "jpeg",
            "quality": 85,
            "scale": 1.5,
            "timeout": cfg.RENDER_TIMEOUT_R2,
        },
    ]
    last_err = None
    for i, strat in enumerate(strategies, 1):
        options = {"fullPage": True, "type": strat["type"]}
        if strat["type"] == "jpeg":
            options["quality"] = strat.get("quality", 85)
        payload = {
            "html": html_content,
            "options": options,
            "viewport": {"width": 1080, "height": 1000, "deviceScaleFactor": strat["scale"]},
            "gotoOptions": {"waitUntil": "networkidle2", "timeout": int(strat["timeout"] * 1000 * 0.8)},
            "bestAttempt": True,
        }
        try:
            async with httpx.AsyncClient(timeout=strat["timeout"] + 15) as client:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
                data = resp.content
            if data and (data.startswith(b"\x89PNG") or data.startswith(b"\xff\xd8")):
                logger.info(f"[group_analysis] 第 {i} 轮渲染成功 ({strat['type']}, {len(data)} bytes)")
                return data
            last_err = RuntimeError(f"渲染返回了无效数据 ({len(data) if data else 0} bytes)")
            logger.warning(f"[group_analysis] 第 {i} 轮渲染数据无效")
        except Exception as e:
            last_err = e
            logger.warning(f"[group_analysis] 第 {i} 轮渲染失败: {e!r}")
    logger.error(f"[group_analysis] 图片渲染全部失败: {last_err!r}")
    return None


async def generate_image_report(
    analysis_result: dict, nickname_map: Dict[str, str],
) -> Tuple[Optional[bytes], str]:
    """生成图片报告，返回 (图片bytes 或 None, html内容)"""
    cfg = get_config()
    template_name = analysis_result.get("report_template") or cfg.REPORT_TEMPLATE
    render_data = await prepare_render_data(analysis_result, nickname_map)
    env = _get_env(template_name)
    html_content = env.get_template("image_template.html").render(**render_data)
    image_data = await html_to_image(html_content)
    return image_data, html_content


async def generate_html_report(
    analysis_result: dict, nickname_map: Dict[str, str], group_id: str,
) -> Optional[Path]:
    """生成 HTML 报告文件"""
    cfg = get_config()
    template_name = analysis_result.get("report_template") or cfg.REPORT_TEMPLATE
    render_data = await prepare_render_data(analysis_result, nickname_map)
    env = _get_env(template_name)
    try:
        html_content = env.get_template("html_template.html").render(**render_data)
    except Exception:
        html_content = env.get_template("image_template.html").render(**render_data)
    out_dir = Path(str(plugin.get_plugin_data_dir())) / "html_reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = f"群聊分析报告_{group_id}_{datetime.now().strftime('%Y%m%d_%H%M')}_{uuid.uuid4().hex[:6]}.html"
    path = out_dir / fname
    await asyncio.to_thread(path.write_text, html_content, encoding="utf-8")
    return path


# ============ 文本报告 ============

def generate_text_report(analysis_result: dict, nickname_map: Dict[str, str]) -> str:
    cfg = get_config()
    stats: GroupStatistics = analysis_result["statistics"]
    topics = analysis_result.get("topics") or []
    user_titles = analysis_result.get("user_titles") or []

    report = f"""
🎯 群聊日常分析报告
📅 {datetime.now().strftime("%Y年%m月%d日")}

📊 基础统计
• 消息总数: {stats.message_count}
• 参与人数: {stats.participant_count}
• 总字符数: {stats.total_characters}
• 表情数量: {stats.emoji_count}
• 最活跃时段: {stats.most_active_period}

💬 热门话题
"""
    for i, topic in enumerate(topics[: cfg.MAX_TOPICS], 1):
        contributors_str = "、".join(topic.contributors)
        report += f"{i}. {topic.topic}\n"
        report += f"   参与者: {contributors_str}\n"
        report += f"   {strip_mentions(topic.detail, nickname_map)}\n\n"

    report += "🏆 群友称号\n"
    for title in user_titles[: cfg.MAX_USER_TITLES]:
        report += f"• {title.name} - {title.title} ({title.mbti})\n"
        report += f"  {strip_mentions(title.reason, nickname_map)}\n\n"

    report += "💬 群圣经\n"
    for i, q in enumerate(stats.golden_quotes[: cfg.MAX_GOLDEN_QUOTES], 1):
        report += f'{i}. "{q.content}" —— {q.sender}\n'
        report += f"   {strip_mentions(q.reason, nickname_map)}\n\n"

    quality: Optional[QualityReview] = analysis_result.get("chat_quality_review")
    if quality and quality.dimensions:
        report += f"🔥 {quality.title}\n{quality.subtitle}\n"
        for d in quality.dimensions:
            report += f"• {d.name} ({d.percentage:.0f}%): {d.comment}\n"
        report += f"📝 {quality.summary}\n"

    return report.strip()
