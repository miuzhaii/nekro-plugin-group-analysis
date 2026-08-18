"""Issue #2 回归：越界配置夹紧，不依赖 Nekro / NoneBot。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pydantic import BaseModel, Field, ValidationError, model_validator

from config_utils import clamp_numeric_mapping, runtime_limit


class DummyConfig(BaseModel):
    MAX_TOPICS: int = Field(default=5, ge=1, le=50)
    MAX_USER_TITLES: int = Field(default=8, ge=1, le=80)
    MAX_GOLDEN_QUOTES: int = Field(default=5, ge=1, le=50)
    ANALYSIS_DAYS: int = Field(default=1, ge=1, le=7)
    PROFILE_IMAGE_OPACITY: float = Field(default=0.12, ge=0.0, le=1.0)

    @model_validator(mode="before")
    @classmethod
    def _clamp(cls, data):
        return clamp_numeric_mapping(data, cls.model_fields)


def test_issue2_values_load():
    m = DummyConfig(MAX_TOPICS=50, MAX_USER_TITLES=80, MAX_GOLDEN_QUOTES=50)
    assert m.MAX_TOPICS == 50
    assert m.MAX_USER_TITLES == 80
    assert m.MAX_GOLDEN_QUOTES == 50


def test_overflow_clamped():
    m = DummyConfig(MAX_TOPICS=999, MAX_USER_TITLES=999, MAX_GOLDEN_QUOTES=999, ANALYSIS_DAYS=30)
    assert (m.MAX_TOPICS, m.MAX_USER_TITLES, m.MAX_GOLDEN_QUOTES, m.ANALYSIS_DAYS) == (50, 80, 50, 7)


def test_underflow_clamped():
    m = DummyConfig(MAX_TOPICS=0, MAX_USER_TITLES=-3, MAX_GOLDEN_QUOTES=0)
    assert (m.MAX_TOPICS, m.MAX_USER_TITLES, m.MAX_GOLDEN_QUOTES) == (1, 1, 1)


def test_string_numbers():
    m = DummyConfig(MAX_TOPICS="50", MAX_USER_TITLES="80", MAX_GOLDEN_QUOTES="50")
    assert (m.MAX_TOPICS, m.MAX_USER_TITLES, m.MAX_GOLDEN_QUOTES) == (50, 80, 50)


def test_missing_uses_defaults():
    m = DummyConfig()
    assert (m.MAX_TOPICS, m.MAX_USER_TITLES, m.MAX_GOLDEN_QUOTES) == (5, 8, 5)


def test_opacity_percent_clamped():
    m = DummyConfig(PROFILE_IMAGE_OPACITY=12)
    assert m.PROFILE_IMAGE_OPACITY == 1.0


def test_invalid_still_raises():
    try:
        DummyConfig(MAX_TOPICS="abc")
    except ValidationError:
        pass
    else:
        raise AssertionError("non-numeric should still fail")
    try:
        DummyConfig(MAX_TOPICS=None)
    except ValidationError:
        pass
    else:
        raise AssertionError("None should still fail")


def test_runtime_hard_cap():
    assert runtime_limit(50, 15) == 15
    assert runtime_limit(8, 20) == 8
    assert runtime_limit(0, 15) == 1
    assert runtime_limit("abc", 15) == 1


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("ALL_PASSED")
