"""配置数值夹紧与运行期硬顶（无 Nekro 依赖，可供单测）。"""

from __future__ import annotations

from typing import Any, Callable, Mapping, MutableMapping, Optional

WarnFn = Callable[[str], None]


def field_bounds(field: Any) -> tuple[Optional[float], Optional[float]]:
    metadata = getattr(field, "metadata", None) or ()
    ge = next((m.ge for m in metadata if hasattr(m, "ge") and m.ge is not None), None)
    le = next((m.le for m in metadata if hasattr(m, "le") and m.le is not None), None)
    return ge, le


def clamp_numeric_mapping(
    data: Any,
    model_fields: Mapping[str, Any],
    *,
    warn: Optional[WarnFn] = None,
) -> Any:
    """把带 ge/le 的数值夹到区间内。非法类型原样交给 pydantic。"""
    if not isinstance(data, MutableMapping):
        return data
    for name, field in model_fields.items():
        if name not in data:
            continue
        ge, le = field_bounds(field)
        if ge is None and le is None:
            continue
        raw = data[name]
        if raw is None or raw == "":
            continue
        is_float = getattr(field, "annotation", int) is float
        try:
            n: float | int = float(raw) if is_float else int(raw)
        except (TypeError, ValueError):
            continue
        clamped = n
        if ge is not None and clamped < ge:
            clamped = float(ge) if is_float else int(ge)
        if le is not None and clamped > le:
            clamped = float(le) if is_float else int(le)
        if clamped != n and warn:
            warn(f"配置 {name}={raw!r} 超出范围 [{ge}, {le}]，已夹紧为 {clamped}")
        data[name] = clamped
    return data


def runtime_limit(value: Any, hard_cap: int, floor: int = 1) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = floor
    return max(floor, min(n, hard_cap))
