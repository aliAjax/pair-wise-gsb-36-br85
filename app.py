"""Cross-region water-right allocation and transfer service (standard library only)."""
from __future__ import annotations

import argparse
import calendar
import json
import os
import sqlite3
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import carryover as co

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "water_rights.db"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_date(value: str, field: str = "日期") -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise DomainError(f"{field}必须是 YYYY-MM-DD") from exc


class DomainError(Exception):
    def __init__(self, message: str, status: int = 400, extra: dict[str, Any] | None = None):
        super().__init__(message)
        self.status = status
        self.extra = extra or {}


class Database:
    def __init__(self, path: str | os.PathLike[str] = DEFAULT_DB):
        self.path = str(path)
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    region TEXT NOT NULL,
                    holder TEXT NOT NULL,
                    priority INTEGER NOT NULL CHECK(priority BETWEEN 1 AND 5),
                    valid_from TEXT NOT NULL,
                    valid_to TEXT NOT NULL,
                    quota REAL NOT NULL CHECK(quota >= 0),
                    used REAL NOT NULL DEFAULT 0 CHECK(used >= 0),
                    created_at TEXT NOT NULL,
                    CHECK(valid_from <= valid_to)
                );
                CREATE TABLE IF NOT EXISTS transfers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    from_account_id INTEGER NOT NULL REFERENCES accounts(id),
                    to_account_id INTEGER NOT NULL REFERENCES accounts(id),
                    amount REAL NOT NULL CHECK(amount > 0),
                    effective_date TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_by TEXT NOT NULL,
                    approved_by TEXT,
                    created_at TEXT NOT NULL,
                    approved_at TEXT
                );
                CREATE TABLE IF NOT EXISTS usage_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER NOT NULL REFERENCES accounts(id),
                    meter_event_id TEXT NOT NULL,
                    amount REAL NOT NULL CHECK(amount > 0),
                    occurred_at TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(account_id, meter_event_id)
                );
                CREATE TABLE IF NOT EXISTS season_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    region TEXT NOT NULL,
                    month INTEGER NOT NULL CHECK(month BETWEEN 1 AND 12),
                    max_fraction REAL NOT NULL CHECK(max_fraction > 0 AND max_fraction <= 1),
                    note TEXT NOT NULL DEFAULT '',
                    UNIQUE(region, month)
                );
                CREATE TABLE IF NOT EXISTS impact_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_region TEXT NOT NULL,
                    target_region TEXT NOT NULL,
                    min_source_fraction REAL NOT NULL CHECK(min_source_fraction >= 0 AND min_source_fraction <= 1),
                    note TEXT NOT NULL DEFAULT '',
                    UNIQUE(source_region, target_region)
                );
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS carryover_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    year INTEGER NOT NULL CHECK(year BETWEEN 2000 AND 2999),
                    created_by TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS carryover_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES carryover_batches(id),
                    account_id INTEGER NOT NULL REFERENCES accounts(id),
                    year INTEGER NOT NULL,
                    quota_snapshot REAL NOT NULL CHECK(quota_snapshot >= 0),
                    used_snapshot REAL NOT NULL CHECK(used_snapshot >= 0),
                    pending_snapshot REAL NOT NULL CHECK(pending_snapshot >= 0),
                    unused REAL NOT NULL CHECK(unused >= 0),
                    cap REAL NOT NULL CHECK(cap >= 0),
                    carry REAL NOT NULL CHECK(carry >= 0),
                    void REAL NOT NULL CHECK(void >= 0),
                    capped INTEGER NOT NULL,
                    UNIQUE(account_id, year)
                );
                CREATE TABLE IF NOT EXISTS annual_pools (
                    account_id INTEGER NOT NULL REFERENCES accounts(id),
                    year INTEGER NOT NULL,
                    permit_quota REAL NOT NULL DEFAULT 0 CHECK(permit_quota >= 0),
                    annual_used REAL NOT NULL DEFAULT 0 CHECK(annual_used >= 0),
                    pool_in REAL NOT NULL DEFAULT 0 CHECK(pool_in >= 0),
                    pool_used REAL NOT NULL DEFAULT 0 CHECK(pool_used >= 0),
                    PRIMARY KEY(account_id, year)
                );
                CREATE TABLE IF NOT EXISTS carryover_draws (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER NOT NULL REFERENCES accounts(id),
                    year INTEGER NOT NULL,
                    meter_event_id TEXT NOT NULL,
                    amount REAL NOT NULL CHECK(amount > 0),
                    annual_taken REAL NOT NULL CHECK(annual_taken >= 0),
                    pool_taken REAL NOT NULL CHECK(pool_taken >= 0),
                    occurred_at TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(account_id, year, meter_event_id)
                );
                """
            )

    def _audit(self, conn: sqlite3.Connection, actor: str, action: str, entity_type: str,
               entity_id: int | None, details: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO audit_log(actor,action,entity_type,entity_id,details,created_at) VALUES(?,?,?,?,?,?)",
            (actor, action, entity_type, entity_id, json.dumps(details, ensure_ascii=False), utcnow()),
        )

    def create_account(self, actor: str, payload: dict[str, Any], role: str = "editor") -> dict[str, Any]:
        if role != "editor":
            raise DomainError("只有配额管理员可以创建账户", 403)
        name = str(payload.get("name", "")).strip()
        region = str(payload.get("region", "")).strip()
        holder = str(payload.get("holder", "")).strip()
        if not name or not region or not holder:
            raise DomainError("账户名称、地区和持有人不能为空")
        try:
            priority = int(payload.get("priority"))
            quota = float(payload.get("quota"))
        except (TypeError, ValueError) as exc:
            raise DomainError("优先级和额度必须是数值") from exc
        if not 1 <= priority <= 5 or quota < 0:
            raise DomainError("优先级应在 1 到 5 之间，额度不能为负")
        valid_from = parse_date(str(payload.get("valid_from", "")), "生效日期")
        valid_to = parse_date(str(payload.get("valid_to", "")), "失效日期")
        if valid_from > valid_to:
            raise DomainError("生效日期不能晚于失效日期")
        with self.connect() as conn:
            try:
                cur = conn.execute(
                    "INSERT INTO accounts(name,region,holder,priority,valid_from,valid_to,quota,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (name, region, holder, priority, valid_from.isoformat(), valid_to.isoformat(), quota, utcnow()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("账户名称已存在", 409) from exc
            self._audit(conn, actor, "account.created", "account", cur.lastrowid, {"name": name, "quota": quota})
            return dict(conn.execute("SELECT * FROM accounts WHERE id=?", (cur.lastrowid,)).fetchone())

    def set_season_rule(self, actor: str, region: str, month: int, max_fraction: float,
                        note: str = "", role: str = "editor") -> dict[str, Any]:
        if role != "editor":
            raise DomainError("只有配额管理员可以设置季节规则", 403)
        if not 1 <= int(month) <= 12 or not 0 < float(max_fraction) <= 1:
            raise DomainError("月份或季节比例不合法")
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO season_rules(region,month,max_fraction,note) VALUES(?,?,?,?)
                   ON CONFLICT(region,month) DO UPDATE SET max_fraction=excluded.max_fraction,note=excluded.note""",
                (region.strip(), int(month), float(max_fraction), note),
            )
            self._audit(conn, actor, "season_rule.saved", "region", None, {"region": region, "month": month, "max_fraction": max_fraction})
        return {"region": region, "month": month, "max_fraction": max_fraction, "note": note}

    def set_impact_rule(self, actor: str, source_region: str, target_region: str, min_source_fraction: float,
                        note: str = "", role: str = "editor") -> dict[str, Any]:
        if role != "editor":
            raise DomainError("只有配额管理员可以设置第三方影响规则", 403)
        if not 0 <= float(min_source_fraction) <= 1:
            raise DomainError("最小留存比例必须在 0 到 1 之间")
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO impact_rules(source_region,target_region,min_source_fraction,note) VALUES(?,?,?,?)
                   ON CONFLICT(source_region,target_region) DO UPDATE SET min_source_fraction=excluded.min_source_fraction,note=excluded.note""",
                (source_region, target_region, float(min_source_fraction), note),
            )
            self._audit(conn, actor, "impact_rule.saved", "region", None, {"source": source_region, "target": target_region, "min_fraction": min_source_fraction})
        return {"source_region": source_region, "target_region": target_region, "min_source_fraction": min_source_fraction, "note": note}

    def _account_row(self, conn: sqlite3.Connection, account_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        if not row:
            raise DomainError("水权账户不存在", 404)
        return row

    def _reserved_outgoing(self, conn: sqlite3.Connection, account_id: int,
                           year: int | None = None) -> float:
        if year is None:
            row = conn.execute(
                "SELECT COALESCE(SUM(amount),0) total FROM transfers WHERE from_account_id=? AND status='pending'",
                (account_id,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COALESCE(SUM(amount),0) total FROM transfers "
                "WHERE from_account_id=? AND status='pending' AND CAST(substr(effective_date,1,4) AS INTEGER)=?",
                (account_id, year),
            ).fetchone()
        return float(row["total"])

    def available(self, account_id: int, as_of: str | None = None) -> dict[str, Any]:
        if as_of:
            parse_date(as_of, "查询日期")
        with self.connect() as conn:
            account = self._account_row(conn, account_id)
            reserved = self._reserved_outgoing(conn, account_id)
            value = max(0.0, float(account["quota"]) - float(account["used"]) - reserved)
        return {"account_id": account_id, "available": value, "reserved_outgoing": reserved, "quota": account["quota"], "used": account["used"]}

    def create_transfer(self, actor: str, payload: dict[str, Any], role: str = "editor") -> dict[str, Any]:
        if role != "editor":
            raise DomainError("只有水权编辑人员可以发起转让", 403)
        try:
            source_id = int(payload.get("from_account_id"))
            target_id = int(payload.get("to_account_id"))
            amount = float(payload.get("amount"))
        except (TypeError, ValueError) as exc:
            raise DomainError("账户和转让量必须是数值") from exc
        if source_id == target_id or amount <= 0:
            raise DomainError("转让账户不能相同，转让量必须大于 0")
        effective = parse_date(str(payload.get("effective_date", "")), "生效日期")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            source = self._account_row(conn, source_id)
            target = self._account_row(conn, target_id)
            year = effective.year
            source_pool = self._pool_row(conn, source_id, year)
            target_pool = self._pool_row(conn, target_id, year)
            pool_year = source_pool is not None and target_pool is not None
            if not pool_year:
                if not (source["valid_from"] <= effective.isoformat() <= source["valid_to"]):
                    raise DomainError("转出账户在生效日无效", 409)
                if not (target["valid_from"] <= effective.isoformat() <= target["valid_to"]):
                    raise DomainError("转入账户在生效日无效", 409)
            if pool_year:
                # 已结转年度以年度台账为准，且结转池水量不参与转让。
                source_permit = float(source_pool["permit_quota"])
                source_used = float(source_pool["annual_used"])
                reserved = self._reserved_outgoing(conn, source_id, year)
                available = source_permit - source_used - reserved
            else:
                source_permit = float(source["quota"])
                reserved = self._reserved_outgoing(conn, source_id)
                available = float(source["quota"]) - float(source["used"]) - reserved
            if amount > available + 1e-9:
                raise DomainError("可用额度不足，待审批转让会预占额度（结转水量不可转让）", 409)
            # More critical users (smaller priority number) cannot transfer their
            # protected allocation to a less critical user.
            if int(source["priority"]) > int(target["priority"]):
                raise DomainError("不能把较低优先级水量转给更高优先级账户", 409)
            impact = conn.execute(
                "SELECT * FROM impact_rules WHERE source_region=? AND target_region=?",
                (source["region"], target["region"]),
            ).fetchone()
            if impact:
                minimum = source_permit * float(impact["min_source_fraction"])
                if available - amount + 1e-9 < minimum:
                    raise DomainError("转让会违反下游第三方最小留存约束", 409)
            cur = conn.execute(
                "INSERT INTO transfers(from_account_id,to_account_id,amount,effective_date,created_by,created_at) VALUES(?,?,?,?,?,?)",
                (source_id, target_id, amount, effective.isoformat(), actor, utcnow()),
            )
            self._audit(conn, actor, "transfer.created", "transfer", cur.lastrowid,
                        {"source": source_id, "target": target_id, "amount": amount, "effective_date": effective.isoformat()})
            return dict(conn.execute("SELECT * FROM transfers WHERE id=?", (cur.lastrowid,)).fetchone())

    def approve_transfer(self, transfer_id: int, actor: str, role: str = "reviewer") -> dict[str, Any]:
        if role != "reviewer":
            raise DomainError("只有审核人可以批准转让", 403)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            transfer = conn.execute("SELECT * FROM transfers WHERE id=?", (transfer_id,)).fetchone()
            if not transfer:
                raise DomainError("转让记录不存在", 404)
            if transfer["status"] != "pending":
                raise DomainError("该转让已处理，不能重复批准", 409)
            if actor == transfer["created_by"]:
                raise DomainError("发起人不能批准自己的转让", 403)
            source = self._account_row(conn, transfer["from_account_id"])
            target = self._account_row(conn, transfer["to_account_id"])
            amount = float(transfer["amount"])
            year = date.fromisoformat(transfer["effective_date"]).year
            source_pool = self._pool_row(conn, source["id"], year)
            target_pool = self._pool_row(conn, target["id"], year)
            pool_year = source_pool is not None and target_pool is not None
            # Compute against pending reservations other than this transfer.
            other_reserved = conn.execute(
                "SELECT COALESCE(SUM(amount),0) total FROM transfers WHERE from_account_id=? AND status='pending' AND id<>?",
                (source["id"], transfer_id),
            ).fetchone()["total"]
            if pool_year:
                source_permit = float(source_pool["permit_quota"])
                available = source_permit - float(source_pool["annual_used"]) - float(other_reserved)
            else:
                source_permit = float(source["quota"])
                available = source_permit - float(source["used"]) - float(other_reserved)
            if amount > available + 1e-9:
                raise DomainError("审批时额度已被其他记录占用，不能批准", 409)
            impact = conn.execute(
                "SELECT * FROM impact_rules WHERE source_region=? AND target_region=?",
                (source["region"], target["region"]),
            ).fetchone()
            if impact:
                minimum = source_permit * float(impact["min_source_fraction"])
                if available - amount + 1e-9 < minimum:
                    raise DomainError("审批时下游最小留存约束不再满足", 409)
            # The approved amount moves between quota balances. Keeping the
            # movement in the quota column preserves the original allocation
            # while making every downstream availability calculation consistent.
            if pool_year:
                # 结转年度的额度移动只作用于当年许可额度，结转池不参与。
                conn.execute(
                    "UPDATE annual_pools SET permit_quota=permit_quota-? WHERE account_id=? AND year=?",
                    (amount, source["id"], year))
                conn.execute(
                    "UPDATE annual_pools SET permit_quota=permit_quota+? WHERE account_id=? AND year=?",
                    (amount, target["id"], year))
            else:
                conn.execute("UPDATE accounts SET quota=quota-? WHERE id=?", (amount, source["id"]))
                conn.execute("UPDATE accounts SET quota=quota+? WHERE id=?", (amount, target["id"]))
            conn.execute("UPDATE transfers SET status='approved',approved_by=?,approved_at=? WHERE id=?", (actor, utcnow(), transfer_id))
            self._audit(conn, actor, "transfer.approved", "transfer", transfer_id, {"amount": amount})
            row = conn.execute("SELECT * FROM transfers WHERE id=?", (transfer_id,)).fetchone()
        return dict(row)

    def reject_transfer(self, transfer_id: int, actor: str, role: str = "reviewer") -> dict[str, Any]:
        if role != "reviewer":
            raise DomainError("只有审核人可以退回转让", 403)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM transfers WHERE id=?", (transfer_id,)).fetchone()
            if not row or row["status"] != "pending":
                raise DomainError("转让不存在或已经处理", 409)
            if actor == row["created_by"]:
                raise DomainError("发起人不能自行退回", 403)
            conn.execute("UPDATE transfers SET status='rejected',approved_by=?,approved_at=? WHERE id=?", (actor, utcnow(), transfer_id))
            self._audit(conn, actor, "transfer.rejected", "transfer", transfer_id, {})
        return {"id": transfer_id, "status": "rejected"}

    def record_usage(self, actor: str, payload: dict[str, Any], role: str = "meter") -> dict[str, Any]:
        if role not in {"meter", "editor"}:
            raise DomainError("只有计量员可以登记取水", 403)
        try:
            account_id = int(payload.get("account_id"))
            amount = float(payload.get("amount"))
        except (TypeError, ValueError) as exc:
            raise DomainError("账户和取水量必须是数值") from exc
        meter_event_id = str(payload.get("meter_event_id", "")).strip()
        occurred = parse_date(str(payload.get("occurred_at", "")), "计量日期")
        if amount <= 0 or not meter_event_id:
            raise DomainError("取水量必须大于 0，计量事件编号不能为空")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            account = self._account_row(conn, account_id)
            year = occurred.year
            pool = self._pool_row(conn, account_id, year)
            reserved = self._reserved_outgoing(conn, account_id, year if pool else None)
            if pool:
                # 年度切换后的台账：取水先扣当年许可额度，再动结转池；
                # 待审批转出预占的是当年许可额度，转让不认结转水量。
                try:
                    draw = co.draw_water(
                        float(pool["permit_quota"]),
                        float(pool["annual_used"]) + reserved,
                        float(pool["pool_in"]),
                        float(pool["pool_used"]),
                        amount,
                    )
                except ValueError as exc:
                    raise DomainError(str(exc), 409) from exc
                month_total = conn.execute(
                    "SELECT COALESCE(SUM(amount),0) total FROM carryover_draws "
                    "WHERE account_id=? AND year=? AND substr(occurred_at,1,7)=?",
                    (account_id, year, occurred.strftime("%Y-%m")),
                ).fetchone()["total"]
            else:
                if not (account["valid_from"] <= occurred.isoformat() <= account["valid_to"]):
                    raise DomainError("取水日期不在许可有效期内", 409)
                available = float(account["quota"]) - float(account["used"]) - reserved
                if amount > available + co.EPS:
                    raise DomainError("取水超过可用额度", 409)
                month_total = conn.execute(
                    "SELECT COALESCE(SUM(amount),0) total FROM usage_records WHERE account_id=? AND substr(occurred_at,1,7)=?",
                    (account_id, occurred.strftime("%Y-%m")),
                ).fetchone()["total"]
            season = conn.execute("SELECT max_fraction FROM season_rules WHERE region=? AND month=?", (account["region"], occurred.month)).fetchone()
            quota_base = float(pool["permit_quota"]) if pool else float(account["quota"])
            if season:
                cap = quota_base * float(season["max_fraction"])
                if float(month_total) + amount > cap + co.EPS:
                    raise DomainError("本次取水超过该月份的季节配额", 409)
            if pool:
                # 计量事件在账户+年份维度幂等，同一事件跨年也不会重复入账。
                try:
                    cur = conn.execute(
                        "INSERT INTO carryover_draws(account_id,year,meter_event_id,amount,annual_taken,pool_taken,occurred_at,actor,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (account_id, year, meter_event_id, amount, draw.annual_taken, draw.pool_taken,
                         occurred.isoformat(), actor, utcnow()),
                    )
                except sqlite3.IntegrityError as exc:
                    raise DomainError("计量事件已登记，不能重复计水", 409) from exc
                conn.execute(
                    "UPDATE annual_pools SET annual_used=annual_used+?, pool_used=pool_used+? WHERE account_id=? AND year=?",
                    (draw.annual_taken, draw.pool_taken, account_id, year),
                )
                self._audit(conn, actor, "usage.recorded", "account", account_id,
                            {"amount": amount, "annual_taken": draw.annual_taken,
                             "pool_taken": draw.pool_taken, "year": year,
                             "occurred_at": occurred.isoformat(), "meter_event_id": meter_event_id})
                row = conn.execute("SELECT * FROM carryover_draws WHERE id=?", (cur.lastrowid,)).fetchone()
                return dict(row)
            try:
                cur = conn.execute(
                    "INSERT INTO usage_records(account_id,meter_event_id,amount,occurred_at,actor,created_at) VALUES(?,?,?,?,?,?)",
                    (account_id, meter_event_id, amount, occurred.isoformat(), actor, utcnow()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("计量事件已登记，不能重复计水", 409) from exc
            conn.execute("UPDATE accounts SET used=used+? WHERE id=?", (amount, account_id))
            self._audit(conn, actor, "usage.recorded", "account", account_id,
                        {"amount": amount, "occurred_at": occurred.isoformat(), "meter_event_id": meter_event_id})
            row = conn.execute("SELECT * FROM usage_records WHERE id=?", (cur.lastrowid,)).fetchone()
        return dict(row)

    def simulate_drought(self, total_supply: float, reduction: float = 0.0, role: str = "viewer") -> dict[str, Any]:
        try:
            total_supply, reduction = float(total_supply), float(reduction)
        except (TypeError, ValueError) as exc:
            raise DomainError("供水量和削减比例必须是数值") from exc
        if total_supply < 0 or not 0 <= reduction < 1:
            raise DomainError("供水量不能为负，削减比例应在 0 到 1 之间")
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM accounts ORDER BY priority,name").fetchall()
        supply = total_supply * (1 - reduction)
        allocation: dict[int, float] = {}
        deficit: dict[int, float] = {}
        remaining = supply
        for priority in range(1, 6):
            group = [r for r in rows if int(r["priority"]) == priority]
            if not group:
                continue
            # During shortage, more critical rights receive their remaining
            # allocation first; only then does water flow to lower priorities.
            requested = sum(max(0.0, float(r["quota"]) - float(r["used"])) for r in group)
            take = min(remaining, requested)
            if requested <= 0:
                continue
            for row in group:
                quota_left = max(0.0, float(row["quota"]) - float(row["used"]))
                share = take * quota_left / requested
                allocation[int(row["id"])] = share
                deficit[int(row["id"])] = quota_left - share
            remaining -= take
            if remaining <= 1e-9:
                for lower in rows:
                    if int(lower["priority"]) > priority:
                        left = max(0.0, float(lower["quota"]) - float(lower["used"]))
                        allocation[int(lower["id"])] = 0.0
                        deficit[int(lower["id"])] = left
                break
        return {"total_supply": total_supply, "reduction": reduction, "effective_supply": supply,
                "unallocated": remaining, "allocations": [
                    {"account_id": int(r["id"]), "name": r["name"], "priority": r["priority"],
                     "allocation": allocation.get(int(r["id"]), 0.0), "deficit": deficit.get(int(r["id"]), 0.0)}
                    for r in rows
                ]}

    def _pool_row(self, conn: sqlite3.Connection, account_id: int, year: int) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM annual_pools WHERE account_id=? AND year=?",
            (account_id, year),
        ).fetchone()

    def list_accounts(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["available"] = max(0.0, float(row["quota"]) - float(row["used"]) - self._reserved_outgoing(conn, int(row["id"])))
                result.append(item)
        return result

    def create_carryover_batch(self, actor: str, payload: dict[str, Any],
                               role: str = "editor") -> dict[str, Any]:
        """为一组账户生成某年度的结转批次。

        同一账户同一年不能生成第二次：重复账户跳过，并在响应中指出原批次。
        """
        if role != "editor":
            raise DomainError("只有配额管理员可以生成结转批次", 403)
        try:
            year = int(payload.get("year"))
        except (TypeError, ValueError) as exc:
            raise DomainError("结转年份必须是整数") from exc
        if not 2000 <= year < 2999:
            raise DomainError("结转年份不合法")
        raw_ids = payload.get("account_ids", [])
        if not isinstance(raw_ids, list) or not raw_ids:
            raise DomainError("必须指定至少一个账户")
        try:
            account_ids = [int(x) for x in raw_ids]
        except (TypeError, ValueError) as exc:
            raise DomainError("账户编号必须是整数") from exc
        if len(set(account_ids)) != len(account_ids):
            raise DomainError("同一账户在本批次中不能重复出现")
        note = str(payload.get("note", "")).strip()
        to_year = year + 1
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            plans: list[tuple[sqlite3.Row, co.CarryoverPlan]] = []
            skipped: list[dict[str, Any]] = []
            for account_id in account_ids:
                account = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
                if not account:
                    raise DomainError(f"水权账户不存在: {account_id}", 404)
                dup = conn.execute(
                    "SELECT e.batch_id FROM carryover_entries e WHERE e.account_id=? AND e.year=?",
                    (account_id, year),
                ).fetchone()
                if dup:
                    skipped.append({"account_id": account_id, "name": account["name"],
                                    "original_batch_id": int(dup["batch_id"])})
                    continue
                # 已用水量取该年实际计量；预占只算当年生效的待审批转出。
                used_row = conn.execute(
                    "SELECT COALESCE(SUM(amount),0) total FROM usage_records "
                    "WHERE account_id=? AND CAST(substr(occurred_at,1,4) AS INTEGER)=?",
                    (account_id, year),
                ).fetchone()
                plan = co.plan_carryover(
                    float(account["quota"]),
                    float(used_row["total"]),
                    self._reserved_outgoing(conn, account_id, year),
                )
                plans.append((account, plan))
            if not plans:
                raise DomainError("该组账户本年度均已结转，不能重复生成", 409,
                                  {"skipped": skipped})
            cur = conn.execute(
                "INSERT INTO carryover_batches(year,created_by,note,created_at) VALUES(?,?,?,?)",
                (year, actor, note, utcnow()),
            )
            batch_id = int(cur.lastrowid)
            entries: list[dict[str, Any]] = []
            for account, plan in plans:
                conn.execute(
                    """INSERT INTO carryover_entries(batch_id,account_id,year,quota_snapshot,used_snapshot,
                       pending_snapshot,unused,cap,carry,void,capped)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (batch_id, int(account["id"]), year, plan.quota, plan.used, plan.pending_out,
                     plan.unused, plan.cap, plan.carry, plan.void, 1 if plan.capped else 0),
                )
                # 下一年台账：结转池累加入账，许可额度沿用当前许可（已批转让已体现在 quota 上）。
                conn.execute(
                    """INSERT INTO annual_pools(account_id,year,permit_quota,annual_used,pool_in,pool_used)
                       VALUES(?,?,?,0,?,0)
                       ON CONFLICT(account_id,year) DO UPDATE SET
                           permit_quota=excluded.permit_quota,
                           pool_in=annual_pools.pool_in+excluded.pool_in""",
                    (int(account["id"]), to_year, plan.quota, plan.carry),
                )
                entries.append({"account_id": int(account["id"]), "name": account["name"],
                                "year": year, "to_year": to_year,
                                "quota": plan.quota, "used": plan.used,
                                "pending_out": plan.pending_out, "unused": plan.unused,
                                "cap": plan.cap, "carry": plan.carry, "void": plan.void,
                                "capped": plan.capped})
            self._audit(conn, actor, "carryover.batch_created", "carryover_batch", batch_id,
                        {"year": year, "account_ids": [int(a["id"]) for a, _ in plans],
                         "skipped": skipped})
        return {"batch_id": batch_id, "year": year, "to_year": to_year,
                "entries": entries, "skipped": skipped}

    def list_carryover_batches(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            batches = conn.execute(
                "SELECT * FROM carryover_batches ORDER BY id DESC").fetchall()
            result = []
            for batch in batches:
                item = dict(batch)
                item["entries"] = [
                    dict(r) for r in conn.execute(
                        """SELECT e.*, a.name AS account_name FROM carryover_entries e
                           JOIN accounts a ON a.id=e.account_id
                           WHERE e.batch_id=? ORDER BY e.id""",
                        (batch["id"],),
                    ).fetchall()
                ]
                result.append(item)
        return result

    def annual_balance(self, account_id: int, year: int) -> dict[str, Any]:
        """展示账户在某年度结转前后的额度结构。"""
        with self.connect() as conn:
            account = self._account_row(conn, account_id)
            pool = self._pool_row(conn, account_id, year)
            if not pool:
                raise DomainError(f"该账户没有 {year} 年度台账", 404)
            pending = self._reserved_outgoing(conn, account_id, year)
            annual_remaining = max(0.0, float(pool["permit_quota"]) - float(pool["annual_used"]))
            pool_remaining = max(0.0, float(pool["pool_in"]) - float(pool["pool_used"]))
            entry = conn.execute(
                "SELECT b.id AS batch_id, b.created_at FROM carryover_entries e "
                "JOIN carryover_batches b ON b.id=e.batch_id WHERE e.account_id=? AND e.year=?",
                (account_id, year - 1),
            ).fetchone()
        return {"account_id": account_id, "name": account["name"], "year": year,
                "permit_quota": float(pool["permit_quota"]),
                "annual_used": float(pool["annual_used"]),
                "annual_remaining": annual_remaining,
                "pool_in": float(pool["pool_in"]),
                "pool_used": float(pool["pool_used"]),
                "pool_remaining": pool_remaining,
                "pending_out": pending,
                # 转让只认当年许可额度，结转池不参与转让。
                "transferable": max(0.0, annual_remaining - pending),
                "available_total": annual_remaining + pool_remaining,
                "carried_from_batch": dict(entry) if entry else None}

    def list_annual_pools(self, year: int | None = None) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if year is None:
                rows = conn.execute(
                    "SELECT p.*, a.name AS account_name FROM annual_pools p "
                    "JOIN accounts a ON a.id=p.account_id ORDER BY p.year,p.account_id").fetchall()
            else:
                rows = conn.execute(
                    "SELECT p.*, a.name AS account_name FROM annual_pools p "
                    "JOIN accounts a ON a.id=p.account_id WHERE p.year=? ORDER BY p.account_id",
                    (year,)).fetchall()
        return [dict(r) for r in rows]

    def list_transfers(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM transfers ORDER BY id DESC").fetchall()
        return [dict(row) for row in rows]

    def audit(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM audit_log ORDER BY id DESC").fetchall()
        return [dict(row) for row in rows]


def seed_demo(db: Database) -> dict[str, int]:
    if db.list_accounts():
        return {str(a["name"]): int(a["id"]) for a in db.list_accounts()}
    upstream = db.create_account("alice", {"name": "北区水库", "region": "upstream", "holder": "北区水务公司", "priority": 1, "valid_from": "2026-01-01", "valid_to": "2026-12-31", "quota": 1000}, "editor")
    downstream = db.create_account("alice", {"name": "河口灌区", "region": "downstream", "holder": "河口合作社", "priority": 2, "valid_from": "2026-01-01", "valid_to": "2026-12-31", "quota": 500}, "editor")
    db.set_season_rule("alice", "upstream", 7, 0.35, "夏季上限", "editor")
    db.set_impact_rule("alice", "upstream", "downstream", 0.4, "保障河口最小生态流量", "editor")
    db.record_usage("meter-01", {"account_id": upstream["id"], "amount": 100, "meter_event_id": "UP-2026-0001", "occurred_at": "2026-03-01"}, "meter")
    return {"北区水库": int(upstream["id"]), "河口灌区": int(downstream["id"])}


class Handler(BaseHTTPRequestHandler):
    db: Database
    server_version = "WaterRights/1.0"

    def _send(self, payload: Any, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _html(self) -> None:
        data = (ROOT / "static" / "index.html").read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as exc:
            raise DomainError("请求体不是合法 JSON") from exc

    def _auth(self) -> tuple[str, str]:
        return self.headers.get("X-User", "anonymous"), self.headers.get("X-Role", "viewer")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path in {"/", "/index.html"}:
                return self._html()
            if parsed.path == "/api/health":
                return self._send({"ok": True})
            if parsed.path == "/api/accounts":
                return self._send({"accounts": self.db.list_accounts()})
            if parsed.path == "/api/transfers":
                return self._send({"transfers": self.db.list_transfers()})
            if parsed.path == "/api/carryover/batches":
                return self._send({"batches": self.db.list_carryover_batches()})
            if parsed.path == "/api/carryover/pools":
                q = parse_qs(parsed.query)
                year = int(q["year"][0]) if q.get("year") else None
                return self._send({"pools": self.db.list_annual_pools(year)})
            if parsed.path == "/api/audit":
                return self._send({"audit": self.db.audit()})
            if parsed.path.startswith("/api/accounts/") and parsed.path.endswith("/available"):
                account_id = int(parsed.path.split("/")[3])
                return self._send(self.db.available(account_id))
            # /api/accounts/{id}/annual-balance?year=2027
            if "/annual-balance" in parsed.path:
                seg = [p for p in parsed.path.split("/") if p]
                q = parse_qs(parsed.query)
                if "year" not in q:
                    raise DomainError("必须提供 year 查询参数")
                return self._send(self.db.annual_balance(int(seg[2]), int(q["year"][0])))
            if parsed.path == "/api/drought/simulate":
                q = parse_qs(parsed.query)
                return self._send(self.db.simulate_drought(float(q.get("supply", ["0"])[0]), float(q.get("reduction", ["0"])[0])))
            raise DomainError("接口不存在", 404)
        except (ValueError, DomainError) as exc:
            payload: dict[str, Any] = {"error": str(exc)}
            payload.update(getattr(exc, "extra", {}))
            self._send(payload, getattr(exc, "status", 400))

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            actor, role = self._auth()
            body = self._body()
            parts = [p for p in parsed.path.split("/") if p]
            if parts == ["api", "accounts"]:
                return self._send(self.db.create_account(actor, body, role), 201)
            if parts == ["api", "rules", "season"]:
                return self._send(self.db.set_season_rule(actor, str(body.get("region", "")), int(body.get("month", 0)), body.get("max_fraction"), str(body.get("note", "")), role), 201)
            if parts == ["api", "rules", "impact"]:
                return self._send(self.db.set_impact_rule(actor, str(body.get("source_region", "")), str(body.get("target_region", "")), body.get("min_source_fraction"), str(body.get("note", "")), role), 201)
            if parts == ["api", "transfers"]:
                return self._send(self.db.create_transfer(actor, body, role), 201)
            if parts == ["api", "carryover", "batches"]:
                return self._send(self.db.create_carryover_batch(actor, body, role), 201)
            if len(parts) == 4 and parts[:2] == ["api", "transfers"] and parts[3] == "approve":
                return self._send(self.db.approve_transfer(int(parts[2]), actor, role))
            if len(parts) == 4 and parts[:2] == ["api", "transfers"] and parts[3] == "reject":
                return self._send(self.db.reject_transfer(int(parts[2]), actor, role))
            if parts == ["api", "usage"]:
                return self._send(self.db.record_usage(actor, body, role), 201)
            raise DomainError("接口不存在", 404)
        except (ValueError, TypeError, DomainError) as exc:
            payload: dict[str, Any] = {"error": str(exc)}
            payload.update(getattr(exc, "extra", {}))
            self._send(payload, getattr(exc, "status", 400))

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[water] {self.address_string()} - {fmt % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description="跨区域水资源使用权分配与转让服务")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8007")))
    parser.add_argument("--db", default=os.getenv("WATER_DB", str(DEFAULT_DB)))
    parser.add_argument("--init", action="store_true", help="创建数据库并写入示例账户")
    args = parser.parse_args()
    db = Database(args.db)
    if args.init:
        seed_demo(db)
        print(f"initialized database at {args.db}")
        return
    Handler.db = db
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"water-rights listening on http://127.0.0.1:{args.port} (db={args.db})")
    server.serve_forever()


if __name__ == "__main__":
    main()
