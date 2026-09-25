"""年度结转纯计算：不依赖数据库，便于独立测试。

规则：
- 结转前先从许可额度中扣掉已用水量和等待审批的转出量，得到剩余水量；
- 最多把许可额度的两成（20%）结入下一年的结转池，其余作废；
- 下一年取水先扣当年额度，再按年份先后动结转池；转让不认结转水量。
"""
from __future__ import annotations

from typing import Any, Iterable

CARRYOVER_CAP_FRACTION = 0.2  # 许可额度的两成


def compute_carryover(quota: float, used: float, reserved: float,
                      cap_fraction: float = CARRYOVER_CAP_FRACTION) -> dict[str, float]:
    """按账户计算结转前后明细。

    返回 unused（剩余水量）、cap（两成上限）、carried（实际结转）、forfeited（作废）。
    """
    unused = max(0.0, float(quota) - float(used) - float(reserved))
    cap = max(0.0, float(quota)) * float(cap_fraction)
    carried = min(unused, cap)
    return {"unused": unused, "cap": cap, "carried": carried, "forfeited": unused - carried}


def plan_drawdown(amount: float, current_available: float,
                  pools: Iterable[Any]) -> tuple[float, list[tuple[int, float]]]:
    """把取水量拆成当年额度部分和结转池部分（先当年、后结转池，按年份升序）。

    pools 中的条目需支持 ["id"] 与 ["remaining"] 取值。调用方需保证总量足够。
    返回 (当年额度承担量, [(结转条目ID, 抽取量), ...])。
    """
    from_current = min(float(amount), max(0.0, float(current_available)))
    rest = float(amount) - from_current
    draws: list[tuple[int, float]] = []
    for pool in pools:
        if rest <= 1e-9:
            break
        take = min(float(pool["remaining"]), rest)
        if take > 0:
            draws.append((int(pool["id"]), take))
            rest -= take
    return from_current, draws
