"""群聊日常分析插件 - 插件定义与配置

功能 1:1 移植自 astrbot_plugin_qq_group_daily_analysis：
统计数据、话题分析、用户称号(MBTI)、群圣经金句、聊天质量锐评、
图片/文本/HTML 报告、定时自动分析、增量(滑动窗口)分析。
"""

from typing import List, Literal, Optional

from nekro_agent.api.plugin import ConfigBase, NekroPlugin
from nekro_agent.core.core_utils import ExtraField
from pydantic import Field

MODEL_GROUP_REF = ExtraField(ref_model_groups=True, model_type="chat").model_dump()

# ============ 默认提示词（与 astrbot 插件 _conf_schema.json 默认值一致） ============

DEFAULT_TOPIC_PROMPT = """请分析接下来提供的群聊记录，提取出最多 **${max_topics}** 个主要话题。根据实际聊天内容提取所有最有意义的话题。

## 对于每个话题，请提供：

1. **话题名称**（突出主题内容，尽量简明扼要，控制在 10 字以内）
2. **主要参与者的用户ID**（最多 5 人，按参与度排序）
3. **话题详细描述**（包含关键信息和结论）

## 注意事项：

- 在生成描述内容时，请务必从你当前人格设定的视角和口吻出发。
- 对于比较有价值的点，稍微用一两句话详细讲讲，让读者能了解讨论的深度
- 对于其中的部分信息，你需要特意提到主题施加的主体是谁，即明确指出"谁做了什么"
- **用户引用**：在话题详情描述中，如果提到了具体用户，请使用 `[用户ID]` 的格式来指代（例如 `[123456]`）。不要只写昵称。我们会自动渲染头像。
- 对于每一条总结，尽量讲清楚前因后果，不要只列出结论
- 如果某个话题有明确的结论或共识，请在描述中体现
- 忽略无意义的闲聊、灌水、单纯的表情回复等
- 优先选择讨论深度较深、参与人数较多的话题
- 如果消息太少或没有明确话题，可以返回空数组 []

群聊记录格式: [HH:MM] [用户ID]: 消息内容

群聊记录：
${messages_text}

---

## 重要：必须返回标准 JSON 格式

严格遵守以下规则：

1. 只使用英文双引号 `"` ，不要使用中文引号 `“` `”`
2. 字符串内容中的引号必须转义为 `\\"`
3. 多个对象之间用逗号分隔
4. 数组元素之间用逗号分隔
5. 不要在 JSON 外添加任何文字说明
6. 描述内容避免使用特殊符号，用普通文字表达

### 返回格式示例：

```json
[
  {{
    "topic": "话题名称",
    "contributors": ["123456789", "987654321"],
    "detail": "话题的详细描述，包含讨论内容、关键信息和结论。注意：在描述中提及用户时，使用 [用户ID] 格式，例如 [123456789]。"
  }},
  {{
    "topic": "另一个话题",
    "contributors": ["111222333", "444555666"],
    "detail": "另一个话题的详细描述..."
  }}
]
```

**注意**：返回的内容必须是纯 JSON，不要包含 markdown 代码块标记或其他格式。"""

DEFAULT_USER_TITLE_PROMPT = """请为以下群友分配合适的称号和 MBTI 类型。

## 规则：

- 每个人只能有一个称号
- 每个称号只能给一个人

## 可选称号：

- **龙王**: 发言频繁但内容轻松的人
- **技术专家**: 经常讨论技术话题的人
- **夜猫子**: 经常在深夜发言的人
- **表情包军火库**: 经常发表情的人
- **沉默终结者**: 经常开启话题的人
- **评论家**: 平均发言长度很长的人
- **阳角**: 在群里很有影响力的人
- **互动达人**: 经常回复别人的人
- *...（你可以自行进行拓展添加）*

## 用户数据：

${users_text}

---

### 返回格式示例：

```json
[
  {{
    "name": "用户名",
    "user_id": "123456789",
    "title": "称号",
    "mbti": "MBTI类型",
    "reason": "获得此称号的原因"
  }}
]
```

**注意**：请以纯 JSON 格式返回，不要包含 markdown 代码块标记。"""

DEFAULT_GOLDEN_QUOTE_PROMPT = """请从以下群聊记录中挑选出 **${max_golden_quotes}** 句最具冲击力、最令人惊叹的「金句」。

## 金句标准：

- **核心标准**：**逆天的神人发言**，即具备颠覆常识的脑洞、逻辑跳脱的表达或强烈反差感的原创内容
- **典型特征**：包含某些争议话题元素、夸张类比、反常规结论、一本正经的「胡说八道」或突破语境的清奇思路，并且具备一定的冲击力，让人印象深刻

## 对于每个金句，请提供：

1. **原文内容**（完整保留发言细节）
2. **发言人用户ID**（必须严格使用消息记录中提供的 [用户ID]）
3. **选择理由**（具体说明其「逆天」之处，如逻辑颠覆点/脑洞角度/反差感/争议话题元素）

## 严格约束：

- 优先筛选 **逆天指数最高** 的内容：
  - 发情、性压抑话题 > 争议话题 > 元素级 > 颠覆认知级 > 逻辑跳脱级 > 趣味调侃级
  - 剔除单纯玩梗或网络热词堆砌的普通发言
- **用户引用**：在选择理由（reason）中，如果提到了具体用户，请使用 `[用户ID]` 的格式来指代（例如 `[123456]`）。不要只写昵称。我们会自动渲染头像。
- **身份对齐**：返回的 `sender` 字段必须是 `[用户ID]` 格式（例如 `[123456]`）。我们会根据 ID 自动还原昵称和头像。

## 群聊记录格式: [HH:MM] [用户ID]: 消息内容

## 群聊记录：

${messages_text}

---

### 返回格式示例：

```json
[
  {{
    "content": "金句原文",
    "sender": "[123456789]",
    "reason": "这句话太逆天了，尤其是对 [987654321] 的逻辑降维打击。"
  }}
]
```

**注意**：返回的内容必须是纯 JSON，不要包含 markdown 代码块标记或其他格式。"""

DEFAULT_QUALITY_PROMPT = """请分析以下群聊记录，输出一份"聊天质量锐评"。

## 任务目标：
1. **维度划分**：将聊天内容划分为 3-6 个【高层级、抽象、泛化】的维度（例如：就业焦虑、生涯规划、技术方案研究、情感树洞、无意义水群等）。
2. **严禁在维度名称（name）中出现任何具体的群聊人物名、项目名、具体的报错内容或细碎的事件点。标题必须保持高度抽象且字数简练（2-6个字）。**
3. 为每个维度计算一个大致的百分比占位（总和小于等于 100%）。
4. **点评内容**：为每个维度写一句符合你当前人格设定的犀利、幽默或温情的点评。具体的吐槽内容、具体的细节事件描述请放在这里。
5. **全群表现**：给出一句总结性的评价，作为总结标题对应的“金句”。
6. **主题设定**：设定一个本次报告的主题标题和副标题。

## 点评风格指南：
- 语言要接地气，多用互联网黑话。吐槽要精准，避重就轻。
- **只有维度名称（name）需要抽象，点评（comment）和总结（summary）可以非常具体和生动。**

## 返回格式要求：
必须以纯 JSON 格式返回，不得包含任何 Markdown 格式。

```json
{{
  "title": "今日群聊主题",
  "subtitle": "副标题",
  "dimensions": [
    {{
      "name": "抽象维度名",
      "percentage": 比例,
      "comment": "与该维度相关的锐评，请务必保持你的人设口吻"
    }}
  ],
  "summary": "一句总结性的金句"
}}
```

群聊记录：
${messages_text}"""

DEFAULT_QUALITY_SUMMARY_PROMPT = """你现在有一份今天全天分散时间段的多个“增量批次点评笔记”。
你的任务是将这些分散的笔记汇总成一份最终的“全天聊天质量终极锐评”。

## 任务目标：
1. **全局抽象维度**：根据各批次的维度表现，平衡权重，提取出 3-6 个覆盖全天的【核心、上层抽象】课题维度（如：职场/行业风向、技术架构演进、社畜心理博弈等）。
2. **严禁在维度名称（name）中出现具体的批次细节。标题必须代表全天的某种趋势。**
3. **百分比融合**：根据全天笔记的频率和强度，给出一个代表全天整体分布的比例（总和不超过100%）。
4. **终极点评**：为每个汇总维度写出一句符合你当前人设且升华后的全天总结性点评。可以融合具体批次中的有趣槽点。
5. **终极总结**：拟定全天的大型主题标题、副标题，并给出一句霸气的全天表现总结。

## 风格要求：
- 只有维度名称（name）需要高度概括抽象。
- 点评（comment）和总结（summary）请尽量生动、具体，要把一整天的梗串联起来。

## 返回格式要求：
必须以纯 JSON 格式返回，不得包含任何 Markdown 格式。

```json
{{
  "title": "今日群聊主题",
  "subtitle": "副标题",
  "dimensions": [
    {{
      "name": "抽象大类标题",
      "percentage": 比例,
      "comment": "全天的锐评总结，请务必保持你的人设口吻"
    }}
  ],
  "summary": "全天总结金句"
}}
```

增量汇总数据：
${reviews_text}"""


plugin = NekroPlugin(
    name="群聊日常分析",
    module_name="group_analysis",
    description="/群分析 指令：基于群聊记录生成精美的日常分析报告（话题总结、用户称号、群圣经、质量锐评），支持定时与增量分析",
    version="1.0.1",
    author="xiaojiu",
    url="https://github.com/miuzhaii/nekro-plugin-group-analysis",
)


@plugin.mount_config()
class GroupAnalysisConfig(ConfigBase):
    # ---------- 基础 ----------
    GROUP_LIST_MODE: Literal["none", "whitelist", "blacklist"] = Field(
        default="none",
        title="群组名单模式",
        description="none=所有群可用 / whitelist=仅白名单群 / blacklist=黑名单群禁用（名单可由 /分析设置 enable|disable 维护）",
    )
    GROUP_LIST: List[str] = Field(
        default=[],
        title="群组名单（初始值）",
        description="白/黑名单初始群号列表；命令修改的结果保存在插件存储中，优先于此配置",
    )
    ANALYSIS_DAYS: int = Field(default=1, title="默认分析天数", ge=1, le=7)
    MAX_MESSAGES: int = Field(default=1000, title="单次最大拉取消息数", ge=50, le=5000)
    MIN_MESSAGES_THRESHOLD: int = Field(
        default=200,
        title="自动分析最小消息数",
        description="自动分析时消息数低于该值则跳过（手动 /群分析 不受限制）",
    )
    BOT_SELF_IDS: List[str] = Field(
        default=[],
        title="机器人账号列表",
        description="这些 QQ 号的消息将被过滤，不参与分析（机器人自身账号会自动加入）",
    )
    OUTPUT_FORMAT: Literal["image", "text", "html"] = Field(
        default="image",
        title="输出格式",
        description="image=图片 / text=文本 / html=HTML文件发送到群文件（可由 /设置格式 修改）",
    )
    REPORT_TEMPLATE: Literal[
        "scrapbook", "retro_futurism", "HatsuneMiku", "hack", "ATRI", "simple", "format", "spring_festival",
    ] = Field(
        default="scrapbook",
        title="报告模板",
        description="报告视觉主题（可由 /设置模板 修改）",
    )

    # ---------- LLM ----------
    MODEL_GROUP: str = Field(
        default="",
        title="LLM 模型组",
        description="使用 nekro-agent 配置的模型组名称，留空则使用主模型组(USE_MODEL_GROUP)",
        json_schema_extra=MODEL_GROUP_REF,
    )
    TOPIC_MODEL_GROUP: str = Field(
        default="", title="话题分析模型组（留空同上）", json_schema_extra=MODEL_GROUP_REF,
    )
    USER_TITLE_MODEL_GROUP: str = Field(
        default="", title="用户称号模型组（留空同上）", json_schema_extra=MODEL_GROUP_REF,
    )
    GOLDEN_QUOTE_MODEL_GROUP: str = Field(
        default="", title="金句分析模型组（留空同上）", json_schema_extra=MODEL_GROUP_REF,
    )
    QUALITY_MODEL_GROUP: str = Field(
        default="", title="质量锐评模型组（留空同上）", json_schema_extra=MODEL_GROUP_REF,
    )
    LLM_RETRIES: int = Field(default=2, title="LLM 重试次数", ge=0, le=5)
    LLM_BACKOFF: int = Field(default=2, title="LLM 重试退避基数（秒）", ge=1, le=30)
    LLM_TIMEOUT: int = Field(default=180, title="LLM 请求超时（秒）", ge=30, le=600)
    LLM_MAX_CONCURRENT: int = Field(default=3, title="LLM 最大并发数", ge=1, le=8)
    ENABLE_STRUCTURED_OUTPUT: bool = Field(
        default=False,
        title="启用结构化输出 (response_format)",
        description="向 LLM 传递 json_schema response_format；接口不支持时自动降级重试",
    )
    PERSONA_PRESET_ID: Optional[int] = Field(
        default=None,
        title="分析人格（人设）",
        description="选择一个人设，分析报告将以该人设口吻输出；为空则不使用人设。若下方『自定义分析人格』填写了内容，则优先使用自定义文本",
        json_schema_extra=ExtraField(ref_presets=True, ref_presets_no_default=True).model_dump(),
    )
    PERSONA_PROMPT: str = Field(
        default="",
        title="自定义分析人格（文本，优先于上方人设）",
        description="作为 system prompt 注入到所有分析请求中；留空则使用上方选择的人设",
        json_schema_extra=ExtraField(is_textarea=True).model_dump(),
    )

    # ---------- 分析功能 ----------
    TOPIC_ENABLED: bool = Field(default=True, title="启用话题分析")
    USER_TITLE_ENABLED: bool = Field(default=True, title="启用用户称号分析")
    GOLDEN_QUOTE_ENABLED: bool = Field(default=True, title="启用金句(群圣经)分析")
    CHAT_QUALITY_ENABLED: bool = Field(default=True, title="启用聊天质量锐评")
    MAX_TOPICS: int = Field(default=5, title="最大话题数", ge=1, le=10)
    MAX_USER_TITLES: int = Field(default=8, title="最大用户称号数", ge=1, le=15)
    MAX_GOLDEN_QUOTES: int = Field(default=5, title="最大金句数", ge=1, le=10)

    # ---------- 定时分析 ----------
    AUTO_ANALYSIS_TIMES: List[str] = Field(
        default=["23:00"],
        title="自动分析时间列表",
        description='每天在这些时间点(HH:MM)自动生成报告，如 ["09:00","23:00"]',
    )
    SCHEDULED_GROUP_LIST_MODE: Literal["whitelist", "blacklist"] = Field(
        default="whitelist",
        title="定时分析名单模式",
        description="whitelist=仅列表内的群自动分析（空列表则不自动分析）/ blacklist=列表外的群都分析",
    )
    SCHEDULED_GROUP_LIST: List[str] = Field(default=[], title="定时分析群列表")
    STAGGER_SECONDS: int = Field(default=30, title="多群错峰间隔（秒）", ge=0, le=300)
    MAX_CONCURRENT_GROUPS: int = Field(default=2, title="最大并发分析群数", ge=1, le=5)

    # ---------- 增量分析 ----------
    INCREMENTAL_GROUP_LIST_MODE: Literal["whitelist", "blacklist"] = Field(
        default="whitelist",
        title="增量分析名单模式",
        description="whitelist=仅列表内的群走增量模式（空列表则不启用增量）/ blacklist=列表外的群都走增量",
    )
    INCREMENTAL_GROUP_LIST: List[str] = Field(default=[], title="增量分析群列表")
    INCREMENTAL_REPORT_IMMEDIATELY: bool = Field(
        default=False, title="增量批次立即报告（调试用）",
    )
    INCREMENTAL_INTERVAL_MINUTES: int = Field(default=120, title="增量分析间隔（分钟）", ge=15, le=720)
    INCREMENTAL_MAX_DAILY: int = Field(default=8, title="每日最大增量分析次数", ge=1, le=48)
    INCREMENTAL_SAFE_LIMIT: int = Field(default=2000, title="增量单批安全消息上限", ge=100, le=10000)
    INCREMENTAL_MIN_MESSAGES: int = Field(default=300, title="增量单批最小消息数", ge=10, le=2000)
    INCREMENTAL_TOPICS_PER_BATCH: int = Field(default=2, title="增量每批最大话题数", ge=1, le=5)
    INCREMENTAL_QUOTES_PER_BATCH: int = Field(default=2, title="增量每批最大金句数", ge=1, le=5)
    INCREMENTAL_ACTIVE_START_HOUR: int = Field(default=8, title="增量活跃时段开始（时）", ge=0, le=23)
    INCREMENTAL_ACTIVE_END_HOUR: int = Field(default=23, title="增量活跃时段结束（时）", ge=0, le=23)
    INCREMENTAL_STAGGER_SECONDS: int = Field(default=2, title="增量多群错峰（秒）", ge=0, le=60)
    INCREMENTAL_FALLBACK_ENABLED: bool = Field(
        default=True, title="增量失败自动回退全量分析",
    )

    # ---------- 图片渲染 ----------
    RENDER_ENDPOINT: str = Field(
        default="http://nekro_html2img:3000",
        title="HTML 渲染服务地址",
        description="browserless/chromium 服务地址（/screenshot API），用于把 HTML 报告渲染为图片",
    )
    RENDER_TOKEN: str = Field(default="", title="渲染服务 TOKEN（可选）")
    RENDER_TIMEOUT_R1: int = Field(default=60, title="第一轮渲染超时（秒）", ge=10, le=300)
    RENDER_TIMEOUT_R2: int = Field(default=120, title="第二轮渲染超时（秒）", ge=10, le=600)
    T2I_FONT_SOURCE: Literal["Mainland", "Overseas"] = Field(
        default="Mainland",
        title="字体源",
        description="Mainland=使用国内镜像加载 Google Fonts / Overseas=直连",
    )
    T2I_MAINLAND_GOOGLE_FONTS: str = Field(default="https://fonts.loli.net", title="国内 Google Fonts 镜像")
    T2I_MAINLAND_GSTATIC: str = Field(default="https://gstatic.loli.net", title="国内 gstatic 镜像")
    T2I_OVERSEAS_GOOGLE_FONTS: str = Field(default="https://fonts.googleapis.com", title="海外 Google Fonts")
    T2I_OVERSEAS_GSTATIC: str = Field(default="https://fonts.gstatic.com", title="海外 gstatic")
    T2I_ATRI_FONT_MIRROR: str = Field(default="https://tc.ciallo.ccwu.cc", title="ATRI 主题字体镜像")
    PROFILE_IMAGE_OPACITY: float = Field(default=0.12, title="称号卡片背景图透明度", ge=0.0, le=1.0)
    PROFILE_IMAGE_SIZE_MODE: str = Field(default="contain", title="称号卡片背景图尺寸模式")

    # ---------- 上传与权限 ----------
    ENABLE_GROUP_FILE_UPLOAD: bool = Field(
        default=False, title="图片报告同时上传群文件",
    )
    GROUP_FILE_FOLDER: str = Field(default="", title="群文件目标文件夹名（留空为根目录）")
    MANAGE_SUPER_ADMINS: List[str] = Field(
        default=["4592474"],
        title="插件管理员 QQ 列表",
        description="可使用 /群分析 等全部命令",
    )
    ALLOW_GROUP_ADMIN_MANAGE: bool = Field(
        default=True,
        title="允许群主/管理员使用命令",
        description="开启后群主和群管理员也可以使用本插件命令",
    )

    # ---------- 提示词 ----------
    TOPIC_PROMPT: str = Field(default=DEFAULT_TOPIC_PROMPT, title="话题分析提示词")
    USER_TITLE_PROMPT: str = Field(default=DEFAULT_USER_TITLE_PROMPT, title="用户称号提示词")
    GOLDEN_QUOTE_PROMPT: str = Field(default=DEFAULT_GOLDEN_QUOTE_PROMPT, title="金句分析提示词")
    QUALITY_PROMPT: str = Field(default=DEFAULT_QUALITY_PROMPT, title="质量锐评提示词")
    QUALITY_SUMMARY_PROMPT: str = Field(default=DEFAULT_QUALITY_SUMMARY_PROMPT, title="质量锐评汇总提示词（增量）")


def get_config() -> GroupAnalysisConfig:
    return plugin.get_config(GroupAnalysisConfig)


config: GroupAnalysisConfig = get_config()
store = plugin.store
