"""LLM 分析器：话题 / 用户称号 / 金句 / 聊天质量锐评

移植自 astrbot 插件 analyzers 系列。LLM 通过 openai SDK 直连
nekro-agent 配置的模型组（OpenAI 兼容接口）。
"""

import asyncio
import re
from string import Template
from typing import Dict, List, Optional, Tuple

import httpx
from nekro_agent.api.core import config as core_config
from nekro_agent.api.core import logger
from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from .jsonutils import (
    extract_golden_quotes_with_regex,
    extract_topics_with_regex,
    extract_user_titles_with_regex,
    parse_json_object_response,
    parse_json_response,
)
from .models import (
    GoldenQuote,
    QualityDimension,
    QualityReview,
    SummaryTopic,
    TokenUsage,
    UserTitle,
)
from .plugin import get_config

QUALITY_COLORS = [
    "#607d8b", "#2196f3", "#f44336", "#e91e63",
    "#ff9800", "#4caf50", "#009688", "#9c27b0",
]

_llm_semaphore: Optional[asyncio.Semaphore] = None


def _get_semaphore() -> asyncio.Semaphore:
    global _llm_semaphore
    if _llm_semaphore is None:
        _llm_semaphore = asyncio.Semaphore(get_config().LLM_MAX_CONCURRENT)
    return _llm_semaphore


def render_prompt(template: str, **kwargs) -> str:
    """提示词渲染：string.Template 的 ${var} 替换，再还原 {{ }} 字面量"""
    text = Template(template).safe_substitute(**kwargs)
    return text.replace("{{", "{").replace("}}", "}")


async def get_persona_prompt() -> str:
    """解析分析人格：自定义文本 > 选中的人设(DBPreset) > 空"""
    cfg = get_config()
    if cfg.PERSONA_PROMPT.strip():
        return cfg.PERSONA_PROMPT.strip()
    pid = str(cfg.PERSONA_PRESET_ID).strip()
    if pid.isdigit():
        try:
            from nekro_agent.models.db_preset import DBPreset

            preset = await DBPreset.get_or_none(id=int(pid))
            if preset and preset.content:
                return str(preset.content)
            logger.warning(f"[group_analysis] 人设 ID {pid} 不存在或内容为空")
        except Exception as e:
            logger.warning(f"[group_analysis] 读取人设失败: {e!r}")
    return ""


def resolve_model_group(override_name: str = ""):
    """解析模型组：插件子配置 > 插件主配置 > nekro 主模型组"""
    cfg = get_config()
    name = (override_name or "").strip() or cfg.MODEL_GROUP.strip() or core_config.USE_MODEL_GROUP
    groups = core_config.MODEL_GROUPS
    if name in groups:
        return name, groups[name]
    if core_config.USE_MODEL_GROUP in groups:
        logger.warning(f"[group_analysis] 模型组 {name} 不存在，回退主模型组")
        return core_config.USE_MODEL_GROUP, groups[core_config.USE_MODEL_GROUP]
    raise RuntimeError(f"模型组 {name} 不存在且无可用主模型组")


def _create_http_client(
    proxy_url: Optional[str],
    read_timeout: int,
    write_timeout: int,
) -> httpx.AsyncClient:
    """创建插件自己的 HTTP 客户端。

    这个 PR 原本复用了 nekro_agent.services.agent.openai._create_http_client，
    但该函数属于 nekro-agent 的内部私有实现，不同版本未必存在。
    如果插件在 import 阶段依赖它，旧版本或部分部署环境会直接 ImportError，
    导致插件还没运行到 LLM 调用就启动失败。

    因此这里保留 PR 的核心行为：
    - 使用模型组的 CHAT_PROXY 代理配置；
    - 沿用插件自己的 LLM_TIMEOUT；
    - 禁用 OpenAI SDK 内置重试后，让 call_llm 统一处理重试和日志摘要；

    同时把 HTTP client 创建逻辑放在插件内，避免绑定 nekro-agent 的私有 API。
    后续如果 nekro-agent 暴露稳定的公共 helper，再考虑切回公共接口。
    """
    timeout = httpx.Timeout(
        timeout=None,
        connect=10.0,
        read=float(read_timeout),
        write=float(write_timeout),
        pool=10.0,
    )
    kwargs = {"timeout": timeout}
    if proxy_url:
        kwargs["proxy"] = proxy_url
    return httpx.AsyncClient(**kwargs)


def _html_to_text(text: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _extract_html_field(pattern: str, text: str) -> str:
    match = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    return _html_to_text(match.group(1))


def _summarize_llm_error(error: Exception) -> str:
    """把上游 HTML 错误页压缩成可读摘要，避免污染日志。"""
    error_type = type(error).__name__
    status_code = getattr(error, "status_code", None)
    raw = str(error)
    lower = raw.lower()

    if "<html" in lower or "<!doctype html" in lower:
        title = _extract_html_field(r"<title[^>]*>(.*?)</title>", raw)
        headline = _extract_html_field(r"<h1[^>]*>(.*?)</h1>", raw)
        subheadline = _extract_html_field(r"<h2[^>]*>(.*?)</h2>", raw)
        ray_id = _extract_html_field(r"Cloudflare Ray ID:\s*<strong[^>]*>(.*?)</strong>", raw)
        details = [part for part in (title, headline, subheadline) if part]
        if "cloudflare" in lower:
            details.append("上游接口返回 Cloudflare 拦截页，请检查模型组 BASE_URL、代理/IP 白名单或服务商风控")
        if ray_id:
            details.append(f"Ray ID: {ray_id}")
        message = "；".join(details) or "上游接口返回 HTML 错误页"
    else:
        message = re.sub(r"\s+", " ", raw).strip()

    if len(message) > 500:
        message = f"{message[:500]}..."
    status = f" status={status_code}" if status_code is not None else ""
    return f"{error_type}{status}: {message}"


async def call_llm(
    prompt: str,
    system_prompt: str = "",
    model_group: str = "",
    response_format: Optional[dict] = None,
    temperature: Optional[float] = None,
) -> Tuple[str, TokenUsage]:
    """调用 LLM（带重试与 response_format 自动降级）"""
    cfg = get_config()
    _, mg = resolve_model_group(model_group)
    use_response_format = response_format if cfg.ENABLE_STRUCTURED_OUTPUT else None
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    last_err: Optional[Exception] = None
    async with (
        _create_http_client(
            proxy_url=(mg.CHAT_PROXY or "").strip() or None,
            read_timeout=cfg.LLM_TIMEOUT,
            write_timeout=cfg.LLM_TIMEOUT,
        ) as http_client,
        AsyncOpenAI(
            api_key=mg.API_KEY,
            base_url=mg.BASE_URL,
            http_client=http_client,
            max_retries=0,
        ) as client,
    ):
        for attempt in range(cfg.LLM_RETRIES + 1):
            try:
                kwargs = {"model": mg.CHAT_MODEL, "messages": messages}
                if temperature is not None:
                    kwargs["temperature"] = temperature
                elif mg.TEMPERATURE is not None:
                    kwargs["temperature"] = mg.TEMPERATURE
                if use_response_format:
                    kwargs["response_format"] = use_response_format
                resp = await client.chat.completions.create(**kwargs)
                text = (resp.choices[0].message.content or "") if resp.choices else ""
                usage = TokenUsage()
                if getattr(resp, "usage", None):
                    usage = TokenUsage(
                        prompt_tokens=getattr(resp.usage, "prompt_tokens", 0) or 0,
                        completion_tokens=getattr(resp.usage, "completion_tokens", 0) or 0,
                        total_tokens=getattr(resp.usage, "total_tokens", 0) or 0,
                    )
                if not text.strip():
                    raise RuntimeError("LLM 返回空内容")
                return text, usage
            except Exception as e:  # noqa: PERF203
                last_err = e
                err_str = str(e).lower()
                err_summary = _summarize_llm_error(e)
                if use_response_format and ("response_format" in err_str or "400" in err_str or "invalid" in err_str):
                    logger.warning(
                        f"[group_analysis] response_format 疑似不被支持，降级为普通输出重试: {err_summary}",
                    )
                    use_response_format = None
                    continue
                if attempt < cfg.LLM_RETRIES:
                    wait = cfg.LLM_BACKOFF * (attempt + 1)
                    logger.warning(
                        f"[group_analysis] LLM 调用失败({attempt + 1})，{wait}s 后重试: {err_summary}",
                    )
                    await asyncio.sleep(wait)
    if last_err is not None:
        raise RuntimeError(f"LLM 调用失败: {_summarize_llm_error(last_err)}") from last_err
    raise RuntimeError("LLM 调用失败")


# ============ 校验模型 ============

class TopicItemModel(BaseModel):
    model_config = ConfigDict(extra="ignore")
    topic: str
    contributors: list
    detail: str

    @field_validator("topic", "detail", mode="before")
    @classmethod
    def _norm_text(cls, v):
        return str(v).strip()

    @field_validator("contributors", mode="before")
    @classmethod
    def _norm_contributors(cls, v):
        if not isinstance(v, list):
            return []
        return [str(i).strip() for i in v if str(i).strip()]


class UserTitleItemModel(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str
    user_id: str
    title: str
    mbti: str
    reason: str

    @field_validator("name", "user_id", "title", "mbti", "reason", mode="before")
    @classmethod
    def _norm_text(cls, v):
        return str(v).strip()


class GoldenQuoteItemModel(BaseModel):
    model_config = ConfigDict(extra="ignore")
    content: str
    sender: str
    reason: str

    @field_validator("content", "sender", "reason", mode="before")
    @classmethod
    def _norm_text(cls, v):
        return str(v).strip()


def _validate_items(model_cls, data_list) -> Tuple[bool, Optional[list], Optional[str]]:
    if not isinstance(data_list, list):
        return False, None, "数据不是列表"
    try:
        normalized = [model_cls.model_validate(item).model_dump() for item in data_list if isinstance(item, dict)]
    except ValidationError as e:
        return False, None, str(e)
    if not normalized:
        return False, None, "列表为空"
    return True, normalized, None


# ============ JSON Schema (structured output) ============

def _response_format(name: str, schema: dict) -> dict:
    return {"type": "json_schema", "json_schema": {"name": name, "strict": False, "schema": schema}}


def topics_response_format(max_count: int) -> dict:
    return _response_format(
        "daily_topics",
        {
            "type": "array",
            "maxItems": max_count,
            "items": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string"},
                    "contributors": {"type": "array", "items": {"type": "string"}},
                    "detail": {"type": "string"},
                },
                "required": ["topic", "contributors", "detail"],
            },
        },
    )


def user_titles_response_format(max_count: int) -> dict:
    return _response_format(
        "daily_user_titles",
        {
            "type": "array",
            "maxItems": max_count,
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "user_id": {"type": "string"},
                    "title": {"type": "string"},
                    "mbti": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["name", "user_id", "title", "mbti", "reason"],
            },
        },
    )


def golden_quotes_response_format(max_count: int) -> dict:
    return _response_format(
        "daily_golden_quotes",
        {
            "type": "array",
            "maxItems": max_count,
            "items": {
                "type": "object",
                "properties": {
                    "content": {"type": "string"},
                    "sender": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["content", "sender", "reason"],
            },
        },
    )


def quality_response_format() -> dict:
    return _response_format(
        "daily_chat_quality",
        {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "subtitle": {"type": "string"},
                "dimensions": {
                    "type": "array",
                    "maxItems": 8,
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "percentage": {"type": "number"},
                            "comment": {"type": "string"},
                        },
                        "required": ["name", "percentage", "comment"],
                    },
                },
                "summary": {"type": "string"},
            },
            "required": ["title", "subtitle", "dimensions", "summary"],
        },
    )


def _messages_to_text(text_messages: List[dict]) -> str:
    return "\n".join(f"[{m['time']}] [{m['user_id']}]: {m['content']}" for m in text_messages)


def _id_nickname_map(text_messages: List[dict]) -> Dict[str, str]:
    return {m["user_id"]: m["sender"] for m in text_messages if m.get("user_id") and m.get("sender")}


# ============ 分析器 ============

async def analyze_topics(
    text_messages: List[dict], max_topics: Optional[int] = None,
) -> Tuple[List[SummaryTopic], TokenUsage]:
    """话题分析"""
    cfg = get_config()
    if not cfg.TOPIC_ENABLED or not text_messages:
        return [], TokenUsage()
    max_count = cfg.effective_max_topics(max_topics)
    prompt = render_prompt(
        cfg.TOPIC_PROMPT, max_topics=max_count, messages_text=_messages_to_text(text_messages),
    )
    try:
        async with _get_semaphore():
            result_text, usage = await call_llm(
                prompt,
                system_prompt=await get_persona_prompt(),
                model_group=cfg.TOPIC_MODEL_GROUP,
                response_format=topics_response_format(max_count),
            )
    except Exception as e:
        logger.error(f"[group_analysis] 话题分析 LLM 调用失败: {e!r}")
        return [], TokenUsage()

    ok, data, err = parse_json_response(result_text, "话题")
    if ok:
        ok, data, err = _validate_items(TopicItemModel, data)
    if not ok:
        data = extract_topics_with_regex(result_text, max_count)
        ok = bool(data)
    if not ok:
        logger.warning(f"[group_analysis] 话题解析失败: {err}; 原文片段: {result_text[:200]}")
        return [], usage

    id_to_nickname = _id_nickname_map(text_messages)
    topics: List[SummaryTopic] = []
    for item in data[:max_count]:
        topic_name = item.get("topic", "").strip()
        detail = item.get("detail", "").strip()
        if not topic_name or not detail:
            continue
        raw_ids = item.get("contributors") or []
        valid_ids = [str(c).strip() for c in raw_ids if str(c).strip().isdigit()]
        resolved = [id_to_nickname.get(uid, uid) for uid in valid_ids] or [
            str(c).strip() for c in raw_ids if str(c).strip()
        ] or ["群友"]
        topics.append(
            SummaryTopic(
                topic=topic_name,
                contributors=resolved[:5],
                detail=detail,
                contributor_ids=valid_ids[:5],
            ),
        )
    return topics, usage


def prepare_user_summaries(user_stats: Dict[str, dict], top_users: List[dict], bot_self_ids: List[str]) -> List[dict]:
    bot_ids = {str(x).strip() for x in bot_self_ids if str(x).strip()}
    target_ids = {str(u["user_id"]) for u in top_users} if top_users else {
        uid for uid, s in user_stats.items() if s.get("message_count", 0) >= 5
    }
    summaries = []
    for user_id, stats in user_stats.items():
        uid = str(user_id)
        if uid in bot_ids or uid not in target_ids:
            continue
        message_count = stats.get("message_count", 0)
        if message_count <= 0:
            continue
        hours = {int(k): v for k, v in (stats.get("hours") or {}).items()}
        night_messages = sum(hours.get(h, 0) for h in range(6))
        summaries.append(
            {
                "name": stats.get("nickname") or uid,
                "user_id": uid,
                "message_count": message_count,
                "avg_chars": round(stats.get("char_count", 0) / message_count, 1),
                "emoji_ratio": round(stats.get("emoji_count", 0) / message_count, 2),
                "night_ratio": round(night_messages / message_count, 2),
                "reply_ratio": round(stats.get("reply_count", 0) / message_count, 2),
            },
        )
    summaries.sort(key=lambda x: x["message_count"], reverse=True)
    return summaries


async def analyze_user_titles(
    user_stats: Dict[str, dict], top_users: List[dict], bot_self_ids: List[str],
) -> Tuple[List[UserTitle], TokenUsage]:
    """用户称号 + MBTI 分析"""
    cfg = get_config()
    if not cfg.USER_TITLE_ENABLED:
        return [], TokenUsage()
    summaries = prepare_user_summaries(user_stats, top_users, bot_self_ids)
    if not summaries:
        return [], TokenUsage()
    users_text = "\n".join(
        f"- {u['name']} (ID:{u['user_id']}): 发言{u['message_count']}条, 平均{u['avg_chars']}字, "
        f"表情比例{u['emoji_ratio']}, 夜间发言比例{u['night_ratio']}, 回复比例{u['reply_ratio']}"
        for u in summaries
    )
    prompt = render_prompt(cfg.USER_TITLE_PROMPT, users_text=users_text)
    max_count = cfg.effective_max_user_titles()
    try:
        async with _get_semaphore():
            result_text, usage = await call_llm(
                prompt,
                system_prompt=await get_persona_prompt(),
                model_group=cfg.USER_TITLE_MODEL_GROUP,
                response_format=user_titles_response_format(max_count),
            )
    except Exception as e:
        logger.error(f"[group_analysis] 用户称号 LLM 调用失败: {e!r}")
        return [], TokenUsage()

    ok, data, err = parse_json_response(result_text, "用户称号")
    if ok:
        ok, data, err = _validate_items(UserTitleItemModel, data)
    if not ok:
        data = extract_user_titles_with_regex(result_text, max_count)
        ok = bool(data)
    if not ok:
        logger.warning(f"[group_analysis] 用户称号解析失败: {err}; 原文片段: {result_text[:200]}")
        return [], usage

    name_by_id = {u["user_id"]: u["name"] for u in summaries}
    titles: List[UserTitle] = []
    for item in data[:max_count]:
        user_id = str(item.get("user_id") or "").strip().strip("[]")
        title = item.get("title", "").strip()
        mbti = item.get("mbti", "").strip()
        reason = item.get("reason", "").strip()
        name = item.get("name", "").strip() or name_by_id.get(user_id, user_id)
        if not name or not title or not mbti or not reason or not user_id:
            continue
        if user_id in name_by_id:
            name = name_by_id[user_id]
        titles.append(UserTitle(name=name, user_id=user_id, title=title, mbti=mbti, reason=reason))
    return titles, usage


async def analyze_golden_quotes(
    text_messages: List[dict], max_quotes: Optional[int] = None,
) -> Tuple[List[GoldenQuote], TokenUsage]:
    """金句（群圣经）分析"""
    cfg = get_config()
    if not cfg.GOLDEN_QUOTE_ENABLED:
        return [], TokenUsage()
    interesting = [m for m in text_messages if 2 <= len(m["content"]) <= 500]
    if not interesting:
        return [], TokenUsage()
    max_count = cfg.effective_max_golden_quotes(max_quotes)
    prompt = render_prompt(
        cfg.GOLDEN_QUOTE_PROMPT, max_golden_quotes=max_count, messages_text=_messages_to_text(interesting),
    )
    try:
        async with _get_semaphore():
            result_text, usage = await call_llm(
                prompt,
                system_prompt=await get_persona_prompt(),
                model_group=cfg.GOLDEN_QUOTE_MODEL_GROUP,
                response_format=golden_quotes_response_format(max_count),
            )
    except Exception as e:
        logger.error(f"[group_analysis] 金句分析 LLM 调用失败: {e!r}")
        return [], TokenUsage()

    ok, data, err = parse_json_response(result_text, "金句")
    if ok:
        ok, data, err = _validate_items(GoldenQuoteItemModel, data)
    if not ok:
        data = extract_golden_quotes_with_regex(result_text, max_count)
        ok = bool(data)
    if not ok:
        logger.warning(f"[group_analysis] 金句解析失败: {err}; 原文片段: {result_text[:200]}")
        return [], usage

    id_to_nickname = _id_nickname_map(interesting)
    quotes: List[GoldenQuote] = []
    for item in data[:max_count]:
        content = item.get("content", "").strip()
        sender = item.get("sender", "").strip()
        reason = item.get("reason", "").strip()
        if not content or not sender:
            continue
        user_id = ""
        potential_id = sender.strip().strip("[]")
        if potential_id in id_to_nickname:
            user_id = potential_id
            sender = id_to_nickname[potential_id]
        quotes.append(GoldenQuote(content=content, sender=sender, reason=reason, user_id=user_id))
    return quotes, usage


def _build_quality_review(data: dict) -> Optional[QualityReview]:
    dims_data = data.get("dimensions") or []
    if not isinstance(dims_data, list) or not dims_data:
        return None
    total = sum(float(d.get("percentage", 0) or 0) for d in dims_data if isinstance(d, dict))
    factor = 100.0 / total if total > 100 else 1.0
    dimensions = []
    for i, d in enumerate(dims_data):
        if not isinstance(d, dict):
            continue
        raw_p = float(d.get("percentage", 0) or 0)
        final_p = round(max(0.0, min(100.0, raw_p)) * factor, 1)
        dimensions.append(
            QualityDimension(
                name=str(d.get("name", "未知")).strip() or "未知",
                percentage=final_p,
                comment=str(d.get("comment", "")).strip(),
                color=QUALITY_COLORS[i % len(QUALITY_COLORS)],
            ),
        )
    if not dimensions:
        return None
    return QualityReview(
        title=str(data.get("title", "")).strip() or "今日群聊质量锐评",
        subtitle=str(data.get("subtitle", "")).strip(),
        dimensions=dimensions,
        summary=str(data.get("summary", "")).strip(),
    )


async def analyze_chat_quality(text_messages: List[dict]) -> Tuple[Optional[QualityReview], TokenUsage]:
    """聊天质量锐评"""
    cfg = get_config()
    if not cfg.CHAT_QUALITY_ENABLED or not text_messages:
        return None, TokenUsage()
    prompt = render_prompt(cfg.QUALITY_PROMPT, messages_text=_messages_to_text(text_messages[:1000]))
    try:
        async with _get_semaphore():
            result_text, usage = await call_llm(
                prompt,
                system_prompt=await get_persona_prompt(),
                model_group=cfg.QUALITY_MODEL_GROUP,
                response_format=quality_response_format(),
            )
    except Exception as e:
        logger.error(f"[group_analysis] 质量锐评 LLM 调用失败: {e!r}")
        return None, TokenUsage()

    ok, data, err = parse_json_object_response(result_text, "质量锐评")
    if not ok:
        logger.warning(f"[group_analysis] 质量锐评解析失败: {err}; 原文片段: {result_text[:200]}")
        return None, usage
    return _build_quality_review(data), usage


async def summarize_quality_reviews(reviews: List[QualityReview]) -> Tuple[Optional[QualityReview], TokenUsage]:
    """增量模式：把多个批次的锐评汇总为全天终极锐评"""
    cfg = get_config()
    if not reviews:
        return None, TokenUsage()
    if len(reviews) == 1:
        return reviews[0], TokenUsage()
    parts = []
    for i, r in enumerate(reviews, 1):
        dims = "; ".join(f"{d.name}({d.percentage}%): {d.comment}" for d in r.dimensions)
        parts.append(f"批次{i}: {r.title} | {r.subtitle}\n维度: {dims}\n总结: {r.summary}")
    prompt = render_prompt(cfg.QUALITY_SUMMARY_PROMPT, reviews_text="\n\n".join(parts))
    try:
        async with _get_semaphore():
            result_text, usage = await call_llm(
                prompt,
                system_prompt=await get_persona_prompt(),
                model_group=cfg.QUALITY_MODEL_GROUP,
                response_format=quality_response_format(),
            )
    except Exception as e:
        logger.error(f"[group_analysis] 质量汇总 LLM 调用失败: {e!r}")
        return reviews[-1], TokenUsage()
    ok, data, err = parse_json_object_response(result_text, "质量汇总")
    if not ok:
        logger.warning(f"[group_analysis] 质量汇总解析失败: {err}")
        return reviews[-1], usage
    return _build_quality_review(data) or reviews[-1], usage
