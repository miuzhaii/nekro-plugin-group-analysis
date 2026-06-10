"""分析数据模型（与 astrbot 插件 data_models.py 一致）"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SummaryTopic:
    topic: str
    contributors: list
    detail: str
    contributor_ids: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "topic": self.topic,
            "contributors": list(self.contributors),
            "detail": self.detail,
            "contributor_ids": list(self.contributor_ids),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SummaryTopic":
        return cls(
            topic=d.get("topic", ""),
            contributors=list(d.get("contributors", [])),
            detail=d.get("detail", ""),
            contributor_ids=list(d.get("contributor_ids", [])),
        )


@dataclass
class UserTitle:
    name: str
    user_id: str
    title: str
    mbti: str
    reason: str

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "user_id": self.user_id,
            "title": self.title,
            "mbti": self.mbti,
            "reason": self.reason,
        }


@dataclass
class GoldenQuote:
    content: str
    sender: str
    reason: str
    user_id: str = ""

    def to_dict(self) -> dict:
        return {
            "content": self.content,
            "sender": self.sender,
            "reason": self.reason,
            "user_id": self.user_id,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "GoldenQuote":
        return cls(
            content=d.get("content", ""),
            sender=d.get("sender", ""),
            reason=d.get("reason", ""),
            user_id=d.get("user_id", ""),
        )


@dataclass
class QualityDimension:
    name: str
    percentage: float
    comment: str
    color: str = "#607d8b"


@dataclass
class QualityReview:
    title: str
    subtitle: str
    dimensions: list
    summary: str

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "subtitle": self.subtitle,
            "dimensions": [
                {
                    "name": d.name,
                    "percentage": d.percentage,
                    "comment": d.comment,
                    "color": d.color,
                }
                for d in self.dimensions
            ],
            "summary": self.summary,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "QualityReview":
        dims = [
            QualityDimension(
                name=x.get("name", "未知"),
                percentage=float(x.get("percentage", 0) or 0),
                comment=x.get("comment", ""),
                color=x.get("color", "#607d8b"),
            )
            for x in d.get("dimensions", [])
        ]
        return cls(
            title=d.get("title", ""),
            subtitle=d.get("subtitle", ""),
            dimensions=dims,
            summary=d.get("summary", ""),
        )


@dataclass
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def add(self, other: "TokenUsage") -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.total_tokens += other.total_tokens


@dataclass
class EmojiStatistics:
    face_count: int = 0
    mface_count: int = 0
    bface_count: int = 0
    sface_count: int = 0
    other_emoji_count: int = 0
    face_details: dict = field(default_factory=dict)

    @property
    def total_emoji_count(self) -> int:
        return (
            self.face_count
            + self.mface_count
            + self.bface_count
            + self.sface_count
            + self.other_emoji_count
        )


@dataclass
class ActivityVisualization:
    hourly_activity: dict = field(default_factory=dict)  # {hour:int -> count}
    daily_activity: dict = field(default_factory=dict)
    user_activity_ranking: list = field(default_factory=list)
    peak_hours: list = field(default_factory=list)
    activity_heatmap_data: dict = field(default_factory=dict)


@dataclass
class GroupStatistics:
    message_count: int = 0
    total_characters: int = 0
    participant_count: int = 0
    most_active_period: str = "未知"
    golden_quotes: list = field(default_factory=list)
    emoji_count: int = 0
    emoji_statistics: EmojiStatistics = field(default_factory=EmojiStatistics)
    activity_visualization: ActivityVisualization = field(default_factory=ActivityVisualization)
    token_usage: TokenUsage = field(default_factory=TokenUsage)
    chat_quality_review: Optional[QualityReview] = None
