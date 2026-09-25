"""年度结转的纯计算规则。

本模块只做算术，不接触数据库、HTTP 或页面，方便单独测试：

- ``plan_carryover``：年度切换时，先扣已用水量和待审批转出量，
  最多把许可额度的两成结入下一年结转池，其余作废。
- ``draw_water``：下一年取水先扣当年许可额度，不足部分再动结转池。
- ``transfer_capacity``：转让只认当年许可额度，结转水量不能转让。
"""
from __future__ import annotations

from dataclasses import dataclass

EPS = 1e-9

# 最多带走许可额度的两成。
CARRY_CAP_FRACTION = 0.2


def _amount(value: float, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field}必须是数值") from exc
    if number < 0:
        raise ValueError(f"{field}不能为负")
    return number


@dataclass(frozen=True)
class CarryoverPlan:
    quota: float          # 许可额度
    used: float           # 已用水量
    pending_out: float    # 等待审批的转出量
    unused: float         # 结转前结余（扣完已用和待批转出，不为负）
    cap: float            # 可结转上限 = 许可额度 × 20%
    carry: float          # 实际带入下一年结转池
    void: float           # 作废额度
    capped: bool          # 结余是否超过两成上限


def plan_carryover(quota: float, used: float, pending_out: float,
                   fraction: float = CARRY_CAP_FRACTION) -> CarryoverPlan:
    """计算单个账户某年度的结转方案。"""
    quota = _amount(quota, "许可额度")
    used = _amount(used, "已用水量")
    pending_out = _amount(pending_out, "待审批转出量")
    if not 0 < float(fraction) <= 1:
        raise ValueError("结转比例必须在 0 到 1 之间")
    # 已用水和等待审批的转出量都不属于“没用完的额度”。
    unused = max(0.0, quota - used - pending_out)
    cap = quota * float(fraction)
    carry = min(unused, cap)
    return CarryoverPlan(
        quota=quota,
        used=used,
        pending_out=pending_out,
        unused=unused,
        cap=cap,
        carry=carry,
        void=unused - carry,
        capped=unused > cap + EPS,
    )


@dataclass(frozen=True)
class WaterDraw:
    amount: float           # 本次取水总量
    annual_taken: float     # 从当年许可额度扣减
    pool_taken: float       # 从结转池扣减
    annual_remaining: float
    pool_remaining: float


def draw_water(annual_quota: float, annual_used: float, pool_in: float,
               pool_used: float, amount: float) -> WaterDraw:
    """按“先当年额度、再结转池”的顺序分配一次取水。"""
    annual_quota = _amount(annual_quota, "当年许可额度")
    annual_used = _amount(annual_used, "当年已用量")
    pool_in = _amount(pool_in, "结转池额度")
    pool_used = _amount(pool_used, "结转池已用量")
    try:
        amount = float(amount)
    except (TypeError, ValueError) as exc:
        raise ValueError("取水量必须是数值") from exc
    if amount <= 0:
        raise ValueError("取水量必须大于 0")
    annual_left = max(0.0, annual_quota - annual_used)
    pool_left = max(0.0, pool_in - pool_used)
    annual_taken = min(amount, annual_left)
    pool_taken = min(amount - annual_taken, pool_left)
    if annual_taken + pool_taken + EPS < amount:
        raise ValueError("当年许可额度与结转池水量均不足")
    return WaterDraw(
        amount=amount,
        annual_taken=annual_taken,
        pool_taken=pool_taken,
        annual_remaining=annual_left - annual_taken,
        pool_remaining=pool_left - pool_taken,
    )


def transfer_capacity(annual_quota: float, annual_used: float,
                      pending_out: float) -> float:
    """可转让额度：只认当年许可额度，结转池水量不计入。"""
    annual_quota = _amount(annual_quota, "当年许可额度")
    annual_used = _amount(annual_used, "当年已用量")
    pending_out = _amount(pending_out, "待审批转出量")
    return max(0.0, annual_quota - annual_used - pending_out)
