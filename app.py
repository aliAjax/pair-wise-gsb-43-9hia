"""Sealed public-procurement tendering and evaluation service."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "public_procurement.db"


class DomainError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_time(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise DomainError("时间格式无效") from exc
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def clean_actor(actor: str) -> str:
    actor = (actor or "").strip()
    if not actor:
        raise DomainError("缺少操作人")
    return actor


def require_role(role: str, allowed: set[str], action: str) -> None:
    if role not in allowed:
        raise DomainError("角色无权执行：%s" % action, 403)


def canonical_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class ProcurementService:
    def __init__(self, db_path: str | os.PathLike[str] = DEFAULT_DB):
        self.db_path = str(db_path)
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tenders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tender_no TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'draft',
                    deadline TEXT NOT NULL,
                    criteria TEXT NOT NULL DEFAULT '[]',
                    evaluation_round INTEGER NOT NULL DEFAULT 1,
                    evaluations_locked INTEGER NOT NULL DEFAULT 0,
                    awarded_bid_id INTEGER,
                    award_snapshot TEXT,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS vendors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    vendor_no TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    representative TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bids (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tender_id INTEGER NOT NULL REFERENCES tenders(id),
                    vendor_id INTEGER NOT NULL REFERENCES vendors(id),
                    consortium_id INTEGER REFERENCES consortia(id),
                    payload TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    price REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'sealed',
                    version INTEGER NOT NULL DEFAULT 1,
                    submitted_by TEXT NOT NULL,
                    submitted_at TEXT NOT NULL,
                    opened_at TEXT,
                    UNIQUE(tender_id,vendor_id)
                );
                CREATE TABLE IF NOT EXISTS evaluations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bid_id INTEGER NOT NULL REFERENCES bids(id),
                    evaluation_round INTEGER NOT NULL,
                    evaluator TEXT NOT NULL,
                    criterion TEXT NOT NULL,
                    raw_value REAL NOT NULL,
                    score REAL NOT NULL,
                    comment TEXT NOT NULL DEFAULT '',
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(bid_id,evaluation_round,evaluator,criterion)
                );
                CREATE TABLE IF NOT EXISTS conflicts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tender_id INTEGER NOT NULL REFERENCES tenders(id),
                    evaluator TEXT NOT NULL,
                    vendor_id INTEGER REFERENCES vendors(id),
                    reason TEXT NOT NULL,
                    declared_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(tender_id,evaluator,vendor_id)
                );
                CREATE TABLE IF NOT EXISTS clarifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tender_id INTEGER NOT NULL REFERENCES tenders(id),
                    vendor_id INTEGER REFERENCES vendors(id),
                    question TEXT NOT NULL,
                    answer TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    answered_by TEXT,
                    created_at TEXT NOT NULL,
                    answered_at TEXT
                );
                CREATE TABLE IF NOT EXISTS complaints (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tender_id INTEGER NOT NULL REFERENCES tenders(id),
                    complainant TEXT NOT NULL,
                    body TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    resolution TEXT,
                    reviewed_by TEXT,
                    created_at TEXT NOT NULL,
                    resolved_at TEXT
                );
                CREATE TABLE IF NOT EXISTS timeline (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tender_id INTEGER REFERENCES tenders(id),
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS vendor_qualifications (
                    vendor_id INTEGER PRIMARY KEY REFERENCES vendors(id),
                    cert_no TEXT NOT NULL DEFAULT '',
                    cert_name TEXT NOT NULL DEFAULT '',
                    expires_at TEXT,
                    suspended INTEGER NOT NULL DEFAULT 0,
                    updated_by TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS consortia (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    consortium_no TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    lead_vendor_id INTEGER NOT NULL REFERENCES vendors(id),
                    members_fingerprint TEXT NOT NULL,
                    member_count INTEGER NOT NULL,
                    snapshot TEXT NOT NULL,
                    snapshot_hash TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    superseded_by_id INTEGER REFERENCES consortia(id),
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(consortium_no,members_fingerprint)
                );
                CREATE TABLE IF NOT EXISTS consortium_members (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    consortium_id INTEGER NOT NULL REFERENCES consortia(id),
                    vendor_id INTEGER NOT NULL REFERENCES vendors(id),
                    role TEXT NOT NULL DEFAULT 'member',
                    share REAL NOT NULL,
                    cert_no TEXT NOT NULL DEFAULT '',
                    cert_name TEXT NOT NULL DEFAULT '',
                    cert_expires_at TEXT,
                    cert_valid INTEGER NOT NULL DEFAULT 1,
                    suspended INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(consortium_id,vendor_id)
                );
                CREATE INDEX IF NOT EXISTS idx_bids_tender ON bids(tender_id,status);
                CREATE INDEX IF NOT EXISTS idx_bids_consortium ON bids(consortium_id);
                CREATE INDEX IF NOT EXISTS idx_eval_bid_round ON evaluations(bid_id,evaluation_round);
                CREATE INDEX IF NOT EXISTS idx_consortium_members_vendor ON consortium_members(vendor_id);
                """
            )
            self._migrate_schema(conn)

    def _migrate_schema(self, conn: sqlite3.Connection) -> None:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(bids)").fetchall()}
        if "consortium_id" not in cols:
            conn.execute("ALTER TABLE bids ADD COLUMN consortium_id INTEGER REFERENCES consortia(id)")

    def _audit(self, conn: sqlite3.Connection, tender_id: int | None, actor: str,
               action: str, details: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO timeline(tender_id,actor,action,details,created_at) VALUES(?,?,?,?,?)",
            (tender_id, actor, action, json.dumps(details, ensure_ascii=False, sort_keys=True), utcnow()),
        )

    def _tender(self, conn: sqlite3.Connection, tender_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM tenders WHERE id=?", (tender_id,)).fetchone()
        if not row:
            raise DomainError("采购项目不存在", 404)
        return row

    def _consortium_row(self, conn: sqlite3.Connection, consortium_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM consortia WHERE id=?", (consortium_id,)).fetchone()
        if not row:
            raise DomainError("联合体版本不存在", 404)
        return row

    def _qualification_state(self, conn: sqlite3.Connection, vendor_id: int,
                             now: datetime | None = None) -> dict[str, Any]:
        """当前资质状态：以最新证书/暂停信息动态判定，证书到期自动失效。"""
        now = now or datetime.now(timezone.utc)
        row = conn.execute("SELECT * FROM vendor_qualifications WHERE vendor_id=?", (vendor_id,)).fetchone()
        if not row:
            return {"vendor_id": vendor_id, "status": "valid", "cert_no": "", "cert_name": "",
                    "expires_at": None, "suspended": False, "reason": None}
        expires_at = parse_time(row["expires_at"]) if row["expires_at"] else None
        if row["suspended"]:
            status, reason = "suspended", "资格已暂停"
        elif expires_at and now >= expires_at:
            status, reason = "expired", "证书已过期"
        else:
            status, reason = "valid", None
        return {"vendor_id": vendor_id, "status": status, "cert_no": row["cert_no"],
                "cert_name": row["cert_name"], "expires_at": row["expires_at"],
                "suspended": bool(row["suspended"]), "reason": reason}

    def _consortium_effective(self, conn: sqlite3.Connection, consortium_id: int,
                              now: datetime | None = None) -> dict[str, Any]:
        """联合体版本有效性 = 结构未被新版本取代 且 每个成员当前资质有效。

        冻结快照只存不改；成员证书过期/暂停时，原版本同样判定失效。
        """
        row = self._consortium_row(conn, consortium_id)
        member_states = []
        blocking_reason = None
        if row["status"] != "active":
            blocking_reason = "联合体版本已被新版本取代（成员已变更）"
        for member in conn.execute("SELECT * FROM consortium_members WHERE consortium_id=? ORDER BY id", (consortium_id,)).fetchall():
            current = self._qualification_state(conn, member["vendor_id"], now)
            frozen = {
                "cert_no": member["cert_no"], "cert_name": member["cert_name"],
                "expires_at": member["cert_expires_at"], "frozen_cert_valid": bool(member["cert_valid"]),
                "frozen_suspended": bool(member["suspended"]),
            }
            if current["status"] != "valid" and not blocking_reason:
                if row["status"] == "active":
                    blocking_reason = "成员 %s %s" % (member["vendor_id"], current["reason"])
            member_states.append({
                "vendor_id": member["vendor_id"], "role": member["role"], "share": member["share"],
                "frozen": frozen, "current_status": current["status"], "current_reason": current["reason"],
            })
        return {"consortium_id": consortium_id, "effective": blocking_reason is None,
                "reason": blocking_reason, "status": row["status"], "members": member_states}

    def _invalidate_sealed_bids(self, conn: sqlite3.Connection, consortium_id: int, reason: str) -> list[int]:
        """未开标的联合体投标整组失效；已开标的保留原快照，仅在评分/授标环节拦截。"""
        rows = conn.execute(
            "SELECT * FROM bids WHERE consortium_id=? AND status='sealed'", (consortium_id,)
        ).fetchall()
        now = utcnow()
        affected = []
        for bid in rows:
            conn.execute("UPDATE bids SET status='invalid',version=version+1 WHERE id=?", (bid["id"],))
            affected.append(bid["id"])
            self._audit(conn, bid["tender_id"], "system", "bid.consortium_invalidated",
                        {"bid_id": bid["id"], "consortium_id": consortium_id, "reason": reason})
        return affected

    def _consortium_summary(self, conn: sqlite3.Connection, consortium_id: int | None,
                            now: datetime | None = None) -> dict[str, Any] | None:
        if not consortium_id:
            return None
        row = conn.execute(
            "SELECT c.*,v.name AS lead_name FROM consortia c JOIN vendors v ON v.id=c.lead_vendor_id WHERE c.id=?",
            (consortium_id,),
        ).fetchone()
        if not row:
            return None
        effective = self._consortium_effective(conn, consortium_id, now)
        return {
            "consortium_id": row["id"], "consortium_no": row["consortium_no"], "version": row["version"],
            "lead_vendor_id": row["lead_vendor_id"], "lead_name": row["lead_name"],
            "structural_status": row["status"], "effective": effective["effective"],
            "invalid_reason": effective["reason"], "snapshot_hash": row["snapshot_hash"],
        }

    def _attach_consortium_summaries(self, conn: sqlite3.Connection, bids: list[dict[str, Any]]) -> None:
        now = datetime.now(timezone.utc)
        cache: dict[int, dict[str, Any]] = {}
        for bid in bids:
            cid = bid.get("consortium_id")
            if not cid:
                bid["consortium"] = None
                continue
            if cid not in cache:
                cache[cid] = self._consortium_summary(conn, cid, now)
            bid["consortium"] = cache[cid]

    def _serialize_consortium(self, conn: sqlite3.Connection, row: sqlite3.Row,
                              now: datetime | None = None) -> dict[str, Any]:
        effective = self._consortium_effective(conn, row["id"], now)
        vendor_rows = {r["id"]: r for r in conn.execute("SELECT * FROM vendors").fetchall()}
        members = []
        for state in effective["members"]:
            vendor = vendor_rows[state["vendor_id"]]
            members.append({
                "vendor_id": state["vendor_id"], "vendor_no": vendor["vendor_no"], "name": vendor["name"],
                "role": state["role"], "share": state["share"], "frozen": state["frozen"],
                "qualification_status": state["current_status"], "qualification_reason": state["current_reason"],
            })
        affected = conn.execute(
            "SELECT b.id,b.tender_id,b.vendor_id,b.price,b.status FROM bids b WHERE b.consortium_id=? ORDER BY b.id",
            (row["id"],),
        ).fetchall()
        return {
            "id": row["id"], "consortium_no": row["consortium_no"], "version": row["version"],
            "lead_vendor_id": row["lead_vendor_id"], "status": row["status"],
            "effective": effective["effective"], "invalid_reason": effective["reason"],
            "snapshot": json.loads(row["snapshot"]), "snapshot_hash": row["snapshot_hash"],
            "members_fingerprint": row["members_fingerprint"], "created_by": row["created_by"],
            "created_at": row["created_at"], "members": members,
            "affected_bids": [dict(r) for r in affected],
        }

    def register_qualification(self, actor: str, role: str, vendor_id: int,
                               cert_no: str, cert_name: str, expires_at: str | None) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"procurement", "supervisor"}, "登记资质证书")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            vendor = conn.execute("SELECT * FROM vendors WHERE id=?", (vendor_id,)).fetchone()
            if not vendor:
                raise DomainError("供应商不存在", 404)
            expires_text = parse_time(expires_at).isoformat(timespec="seconds") if expires_at else None
            conn.execute(
                """INSERT INTO vendor_qualifications(vendor_id,cert_no,cert_name,expires_at,suspended,updated_by,updated_at)
                   VALUES(?,?,?,? ,0,?,?)
                   ON CONFLICT(vendor_id) DO UPDATE SET cert_no=excluded.cert_no,cert_name=excluded.cert_name,
                       expires_at=excluded.expires_at,updated_by=excluded.updated_by,updated_at=excluded.updated_at""",
                (vendor_id, cert_no.strip(), cert_name.strip(), expires_text, actor, utcnow()),
            )
            self._audit(conn, None, actor, "qualification.registered",
                        {"vendor_id": vendor_id, "cert_no": cert_no.strip(), "expires_at": expires_text})
            return self._qualification_state(conn, vendor_id)

    def set_qualification_suspension(self, actor: str, role: str, vendor_id: int,
                                     suspended: bool, reason: str = "") -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"procurement", "supervisor"}, "暂停或恢复资格")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            vendor = conn.execute("SELECT * FROM vendors WHERE id=?", (vendor_id,)).fetchone()
            if not vendor:
                raise DomainError("供应商不存在", 404)
            row = conn.execute("SELECT * FROM vendor_qualifications WHERE vendor_id=?", (vendor_id,)).fetchone()
            if not row:
                raise DomainError("供应商尚未登记资质证书", 409)
            if bool(row["suspended"]) == bool(suspended):
                raise DomainError("资格暂停状态未变化", 409)
            conn.execute(
                "UPDATE vendor_qualifications SET suspended=?,updated_by=?,updated_at=? WHERE vendor_id=?",
                (1 if suspended else 0, actor, utcnow(), vendor_id),
            )
            invalidated: list[int] = []
            if suspended:
                links = conn.execute(
                    "SELECT DISTINCT consortium_id FROM consortium_members WHERE vendor_id=?", (vendor_id,)
                ).fetchall()
                for link in links:
                    invalidated.extend(self._invalidate_sealed_bids(conn, link["consortium_id"], "成员资格暂停"))
            self._audit(conn, None, actor, "qualification.suspension_changed",
                        {"vendor_id": vendor_id, "suspended": bool(suspended),
                         "reason": reason.strip(), "invalidated_bids": invalidated})
            return self._qualification_state(conn, vendor_id)

    def register_consortium(self, actor: str, role: str, consortium_no: str,
                            lead_vendor_id: int, members: list[dict[str, Any]]) -> dict[str, Any]:
        """提交联合体即冻结一个新版本：牵头方、成员份额、资质快照。

        成员变更只能生成新版本；相同牵头方+成员+份额的重复提交幂等只留一条。
        """
        actor = clean_actor(actor)
        require_role(role, {"vendor", "procurement", "supervisor"}, "提交联合体")
        if not consortium_no.strip():
            raise DomainError("联合体编号不能为空")
        if not isinstance(members, list) or not members:
            raise DomainError("联合体成员不能为空")
        normalized: list[dict[str, Any]] = []
        total_share = Decimal("0")
        seen: set[int] = set()
        for item in members:
            if not isinstance(item, dict):
                raise DomainError("联合体成员格式无效")
            try:
                vendor_id = int(item["vendor_id"])
                share = Decimal(str(item["share"]))
            except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
                raise DomainError("联合体成员或份额无效") from exc
            if vendor_id in seen:
                raise DomainError("联合体成员不能重复")
            seen.add(vendor_id)
            if share < 0 or share > 100:
                raise DomainError("联合体成员份额必须在 0 到 100 之间")
            total_share += share
            normalized.append({"vendor_id": vendor_id, "share": share})
        if total_share != 100:
            raise DomainError("联合体成员份额合计必须等于100")
        if lead_vendor_id not in seen:
            raise DomainError("牵头方必须是联合体成员")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            vendors = {}
            for item in normalized:
                vendor = conn.execute("SELECT * FROM vendors WHERE id=?", (item["vendor_id"],)).fetchone()
                if not vendor:
                    raise DomainError("供应商不存在: %s" % item["vendor_id"], 404)
                vendors[item["vendor_id"]] = vendor
            frozen_members = []
            now = datetime.now(timezone.utc)
            for item in normalized:
                qual = self._qualification_state(conn, item["vendor_id"], now)
                member_role = "lead" if item["vendor_id"] == lead_vendor_id else "member"
                frozen_members.append({
                    "vendor_id": item["vendor_id"], "vendor_no": vendors[item["vendor_id"]]["vendor_no"],
                    "name": vendors[item["vendor_id"]]["name"], "role": member_role,
                    "share": float(item["share"]),
                    "qualification": {"status": qual["status"], "cert_no": qual["cert_no"],
                                      "cert_name": qual["cert_name"], "expires_at": qual["expires_at"],
                                      "suspended": qual["suspended"]},
                })
            ordered = sorted(frozen_members, key=lambda m: m["vendor_id"])
            fingerprint_material = [
                {"vendor_id": m["vendor_id"], "role": m["role"], "share": m["share"]} for m in ordered
            ]
            fingerprint = canonical_hash(fingerprint_material)
            existing = conn.execute(
                "SELECT * FROM consortia WHERE consortium_no=? AND members_fingerprint=?",
                (consortium_no.strip(), fingerprint),
            ).fetchone()
            if existing:
                return {"deduplicated": True, **self._serialize_consortium(conn, existing, now)}
            latest = conn.execute(
                "SELECT MAX(version) AS v FROM consortia WHERE consortium_no=?", (consortium_no.strip(),)
            ).fetchone()["v"] or 0
            snapshot = {
                "consortium_no": consortium_no.strip(), "version": latest + 1,
                "lead_vendor_id": lead_vendor_id,
                "lead_name": vendors[lead_vendor_id]["name"],
                "members": frozen_members, "frozen_by": actor,
                "frozen_at": utcnow(),
            }
            snapshot_text = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
            snapshot_hash = canonical_hash(snapshot)
            cur = conn.execute(
                """INSERT INTO consortia(consortium_no,version,lead_vendor_id,members_fingerprint,member_count,
                       snapshot,snapshot_hash,created_by,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (consortium_no.strip(), latest + 1, lead_vendor_id, fingerprint, len(normalized),
                 snapshot_text, snapshot_hash, actor, utcnow()),
            )
            consortium_id = cur.lastrowid
            for member in frozen_members:
                qual = member["qualification"]
                conn.execute(
                    """INSERT INTO consortium_members(consortium_id,vendor_id,role,share,cert_no,cert_name,
                           cert_expires_at,cert_valid,suspended)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (consortium_id, member["vendor_id"], member["role"], member["share"],
                     qual["cert_no"], qual["cert_name"], qual["expires_at"],
                     1 if qual["status"] != "expired" else 0, 1 if qual["suspended"] else 0),
                )
            previous = conn.execute(
                "SELECT * FROM consortia WHERE consortium_no=? AND status='active' AND id<>? ORDER BY version",
                (consortium_no.strip(), consortium_id),
            ).fetchall()
            superseded_ids = []
            for old in previous:
                conn.execute("UPDATE consortia SET status='superseded',superseded_by_id=? WHERE id=?",
                             (consortium_id, old["id"]))
                superseded_ids.append(old["id"])
                self._invalidate_sealed_bids(conn, old["id"], "联合体成员变更，已被新版本取代")
            self._audit(conn, None, actor, "consortium.version_frozen",
                        {"consortium_id": consortium_id, "consortium_no": consortium_no.strip(),
                         "version": latest + 1, "lead_vendor_id": lead_vendor_id,
                         "members": fingerprint_material, "superseded": superseded_ids,
                         "snapshot_hash": snapshot_hash})
            row = self._consortium_row(conn, consortium_id)
            return {"deduplicated": False, **self._serialize_consortium(conn, row, now)}

    def list_consortia(self, actor: str, role: str, consortium_no: str | None = None) -> dict[str, Any]:
        with self.connect() as conn:
            now = datetime.now(timezone.utc)
            if consortium_no:
                rows = conn.execute(
                    "SELECT * FROM consortia WHERE consortium_no=? ORDER BY version", (consortium_no.strip(),)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM consortia ORDER BY id DESC").fetchall()
            versions = [self._serialize_consortium(conn, row, now) for row in rows]
            if role == "vendor":
                mine = {r["vendor_id"] for r in conn.execute(
                    """SELECT DISTINCT cm.vendor_id FROM consortium_members cm
                       JOIN vendors v ON v.vendor_no=? WHERE cm.vendor_id=v.id""", (actor,)).fetchall()}
                if not mine:
                    try:
                        vendor = conn.execute("SELECT id FROM vendors WHERE vendor_no=?", (actor,)).fetchone()
                        mine = {vendor["id"]} if vendor else set()
                    except Exception:
                        mine = set()
                versions = [c for c in versions
                            if any(m["vendor_id"] in mine for m in c["members"])]
            elif role not in {"procurement", "supervisor", "auditor"}:
                versions = []
            return {"consortia": versions}

    def get_consortium(self, actor: str, role: str, consortium_id: int) -> dict[str, Any]:
        with self.connect() as conn:
            data = self._serialize_consortium(conn, self._consortium_row(conn, consortium_id))
            if role in {"procurement", "supervisor", "auditor"}:
                return data
            if role == "vendor":
                vendor = conn.execute("SELECT id FROM vendors WHERE vendor_no=?", (actor,)).fetchone()
                member_ids = {m["vendor_id"] for m in data["members"]}
                if vendor and vendor["id"] in member_ids:
                    return data
            raise DomainError("无权查看该联合体版本", 403)

    def create_vendor(self, actor: str, role: str, vendor_no: str, name: str,
                      representative: str) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"procurement", "supervisor"}, "创建供应商")
        if not vendor_no.strip() or not name.strip() or not representative.strip():
            raise DomainError("供应商编号、名称和代表不能为空")
        with self.connect() as conn:
            try:
                cur = conn.execute(
                    "INSERT INTO vendors(vendor_no,name,representative,created_at) VALUES(?,?,?,?)",
                    (vendor_no.strip(), name.strip(), representative.strip(), utcnow()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("供应商编号已存在", 409) from exc
            self._audit(conn, None, actor, "vendor.created", {"vendor_no": vendor_no.strip()})
            return dict(conn.execute("SELECT * FROM vendors WHERE id=?", (cur.lastrowid,)).fetchone())

    def create_tender(self, actor: str, role: str, tender_no: str, title: str,
                      deadline: str, criteria: list[dict[str, Any]], description: str = "") -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"procurement"}, "创建采购项目")
        parse_time(deadline)
        if not tender_no.strip() or not title.strip():
            raise DomainError("项目编号和标题不能为空")
        normalized_criteria = []
        total_weight = Decimal("0")
        for item in criteria:
            if not isinstance(item, dict) or not str(item.get("name", "")).strip():
                raise DomainError("评分项格式无效")
            kind = item.get("kind", "direct")
            if kind not in {"direct", "cost"}:
                raise DomainError("评分项类型只支持 direct 或 cost")
            try:
                weight = Decimal(str(item["weight"]))
                max_value = Decimal(str(item.get("max_value", 100)))
            except (KeyError, InvalidOperation) as exc:
                raise DomainError("评分权重或上限无效") from exc
            if weight <= 0 or max_value <= 0:
                raise DomainError("评分权重和上限必须大于0")
            total_weight += weight
            normalized_criteria.append({"name": str(item["name"]).strip(), "kind": kind,
                                        "weight": float(weight), "max_value": float(max_value)})
        if not normalized_criteria or total_weight != 100:
            raise DomainError("评分项权重合计必须等于100")
        with self.connect() as conn:
            try:
                cur = conn.execute(
                    """INSERT INTO tenders(tender_no,title,description,deadline,criteria,created_by,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (tender_no.strip(), title.strip(), description.strip(), parse_time(deadline).isoformat(timespec="seconds"),
                     json.dumps(normalized_criteria, ensure_ascii=False), actor, utcnow(), utcnow()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("项目编号已存在", 409) from exc
            self._audit(conn, cur.lastrowid, actor, "tender.created", {"tender_no": tender_no.strip()})
            return dict(self._tender(conn, cur.lastrowid))

    def publish_tender(self, actor: str, role: str, tender_id: int, expected_version: int) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"procurement"}, "发布采购项目")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            tender = self._tender(conn, tender_id)
            if tender["status"] != "draft":
                raise DomainError("只有草稿项目可以发布", 409)
            if tender["version"] != int(expected_version):
                raise DomainError("项目已变化，请刷新后重试", 409)
            conn.execute("UPDATE tenders SET status='published',version=version+1,updated_at=? WHERE id=?", (utcnow(), tender_id))
            self._audit(conn, tender_id, actor, "tender.published", {"deadline": tender["deadline"]})
            return dict(self._tender(conn, tender_id))

    def submit_bid(self, actor: str, role: str, tender_id: int, vendor_id: int,
                   payload: dict[str, Any], price: float, expected_version: int | None = None,
                   consortium_id: int | None = None) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"vendor"}, "提交投标")
        if not isinstance(payload, dict):
            raise DomainError("投标内容必须是对象")
        try:
            price = float(price)
        except (TypeError, ValueError) as exc:
            raise DomainError("报价必须是数值") from exc
        if price <= 0:
            raise DomainError("报价必须大于0")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            tender = self._tender(conn, tender_id)
            if tender["status"] != "published":
                raise DomainError("当前项目不接受投标", 409)
            if datetime.now(timezone.utc) >= parse_time(tender["deadline"]):
                raise DomainError("投标截止时间已过", 409)
            vendor = conn.execute("SELECT * FROM vendors WHERE id=?", (vendor_id,)).fetchone()
            if not vendor:
                raise DomainError("供应商不存在", 404)
            consortium_effective: dict[str, Any] | None = None
            if consortium_id is not None:
                consortium = self._consortium_row(conn, int(consortium_id))
                consortium_effective = self._consortium_effective(conn, consortium["id"])
                if consortium["lead_vendor_id"] != vendor_id:
                    raise DomainError("只有联合体牵头方可以提交投标", 403)
                if not consortium_effective["effective"]:
                    raise DomainError("联合体版本已失效：%s" % consortium_effective["reason"], 409)
            if not conn.execute("SELECT 1 FROM conflicts WHERE tender_id=? AND vendor_id=? AND evaluator=?", (tender_id, vendor_id, actor)).fetchone():
                pass
            existing = conn.execute("SELECT * FROM bids WHERE tender_id=? AND vendor_id=?", (tender_id, vendor_id)).fetchone()
            payload_text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            digest = canonical_hash(payload)
            if existing:
                if existing["status"] != "sealed":
                    raise DomainError("投标已撤回或已开标，不能修改", 409)
                if expected_version is None or existing["version"] != int(expected_version):
                    raise DomainError("投标已变化，请刷新后重试", 409)
                conn.execute(
                    "UPDATE bids SET payload=?,payload_hash=?,price=?,consortium_id=?,version=version+1,submitted_at=? WHERE id=? AND version=?",
                    (payload_text, digest, price, consortium_id if consortium_id is not None else existing["consortium_id"],
                     utcnow(), existing["id"], expected_version),
                )
                bid_id = existing["id"]
                action = "bid.updated"
            else:
                cur = conn.execute(
                    """INSERT INTO bids(tender_id,vendor_id,consortium_id,payload,payload_hash,price,submitted_by,submitted_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (tender_id, vendor_id, consortium_id, payload_text, digest, price, actor, utcnow()),
                )
                bid_id = cur.lastrowid
                action = "bid.submitted"
            audit_details = {"bid_id": bid_id, "vendor_id": vendor_id, "hash": digest}
            if consortium_id is not None:
                audit_details["consortium_id"] = consortium_id
                audit_details["snapshot_hash"] = consortium["snapshot_hash"]
            self._audit(conn, tender_id, actor, action, audit_details)
            bid = dict(conn.execute("SELECT * FROM bids WHERE id=?", (bid_id,)).fetchone())
            bid["payload_hash"] = digest
            bid["consortium"] = self._consortium_summary(conn, bid["consortium_id"])
            return bid

    def withdraw_bid(self, actor: str, role: str, bid_id: int, expected_version: int) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"vendor"}, "撤回投标")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            bid = conn.execute("SELECT * FROM bids WHERE id=?", (bid_id,)).fetchone()
            if not bid:
                raise DomainError("投标不存在", 404)
            tender = self._tender(conn, bid["tender_id"])
            if bid["submitted_by"] != actor:
                raise DomainError("只能撤回自己的投标", 403)
            if bid["version"] != int(expected_version):
                raise DomainError("投标已变化，请刷新后重试", 409)
            if datetime.now(timezone.utc) >= parse_time(tender["deadline"]) or bid["status"] != "sealed":
                raise DomainError("截止后不能撤回投标", 409)
            conn.execute("UPDATE bids SET status='withdrawn',version=version+1 WHERE id=?", (bid_id,))
            self._audit(conn, bid["tender_id"], actor, "bid.withdrawn", {"bid_id": bid_id})
            return dict(conn.execute("SELECT * FROM bids WHERE id=?", (bid_id,)).fetchone())

    def open_bids(self, actor: str, role: str, tender_id: int, expected_version: int) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"procurement", "supervisor"}, "开标")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            tender = self._tender(conn, tender_id)
            if tender["status"] != "published":
                raise DomainError("项目当前不能开标", 409)
            if tender["version"] != int(expected_version):
                raise DomainError("项目已变化，请刷新后重试", 409)
            if datetime.now(timezone.utc) < parse_time(tender["deadline"]):
                raise DomainError("尚未到开标时间", 409)
            rows = conn.execute("SELECT * FROM bids WHERE tender_id=? AND status='sealed' ORDER BY id", (tender_id,)).fetchall()
            opened = []
            blocked = []
            now = utcnow()
            now_dt = datetime.now(timezone.utc)
            for row in rows:
                if row["consortium_id"]:
                    effective = self._consortium_effective(conn, row["consortium_id"], now_dt)
                    consortium = self._consortium_row(conn, row["consortium_id"])
                    if canonical_hash(json.loads(consortium["snapshot"])) != consortium["snapshot_hash"]:
                        raise DomainError("联合体资质快照完整性校验失败: 投标 %s" % row["id"], 409)
                    if not effective["effective"]:
                        conn.execute("UPDATE bids SET status='invalid',version=version+1 WHERE id=?", (row["id"],))
                        blocked.append({"bid_id": row["id"], "consortium_id": row["consortium_id"],
                                        "reason": effective["reason"]})
                        self._audit(conn, tender_id, actor, "bid.consortium_blocked_at_open",
                                    {"bid_id": row["id"], "consortium_id": row["consortium_id"],
                                     "reason": effective["reason"]})
                        continue
                digest = canonical_hash(json.loads(row["payload"]))
                if digest != row["payload_hash"]:
                    raise DomainError("投标完整性校验失败: %s" % row["id"], 409)
                conn.execute("UPDATE bids SET status='opened',opened_at=?,version=version+1 WHERE id=?", (now, row["id"]))
                opened.append(dict(conn.execute("SELECT * FROM bids WHERE id=?", (row["id"],)).fetchone()))
            conn.execute("UPDATE tenders SET status='opened',version=version+1,updated_at=? WHERE id=?", (now, tender_id))
            self._audit(conn, tender_id, actor, "tender.opened",
                        {"bid_count": len(opened), "blocked_consortium_bids": blocked})
            result = {"tender": dict(self._tender(conn, tender_id)), "bids": opened}
            if blocked:
                result["blocked_bids"] = blocked
            return result

    def declare_conflict(self, actor: str, role: str, tender_id: int, evaluator: str,
                         vendor_id: int | None, reason: str) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"evaluator", "procurement", "supervisor"}, "申报利益冲突")
        if not evaluator.strip() or not reason.strip():
            raise DomainError("评审人和冲突原因不能为空")
        with self.connect() as conn:
            self._tender(conn, tender_id)
            try:
                cur = conn.execute(
                    "INSERT INTO conflicts(tender_id,evaluator,vendor_id,reason,declared_by,created_at) VALUES(?,?,?,?,?,?)",
                    (tender_id, evaluator.strip(), vendor_id, reason.strip(), actor, utcnow()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("利益冲突已申报", 409) from exc
            self._audit(conn, tender_id, actor, "conflict.declared", {"evaluator": evaluator.strip(), "vendor_id": vendor_id, "reason": reason.strip()})
            return dict(conn.execute("SELECT * FROM conflicts WHERE id=?", (cur.lastrowid,)).fetchone())

    def evaluate_bid(self, actor: str, role: str, bid_id: int, values: dict[str, float],
                     comment: str = "") -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"evaluator"}, "评分")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            bid = conn.execute("SELECT * FROM bids WHERE id=?", (bid_id,)).fetchone()
            if not bid:
                raise DomainError("投标不存在", 404)
            tender = self._tender(conn, bid["tender_id"])
            if tender["status"] not in {"opened", "reevaluation"} or tender["evaluations_locked"]:
                raise DomainError("当前项目不能评分", 409)
            if bid["status"] not in {"opened", "qualified"}:
                raise DomainError("该投标不能评分", 409)
            related_vendor_ids = {bid["vendor_id"]}
            if bid["consortium_id"]:
                effective = self._consortium_effective(conn, bid["consortium_id"])
                if not effective["effective"]:
                    raise DomainError("联合体版本已失效，保留开标快照但不能评分：%s" % effective["reason"], 409)
                related_vendor_ids.update(m["vendor_id"] for m in effective["members"])
            placeholders = ",".join("?" for _ in related_vendor_ids)
            conflict = conn.execute(
                "SELECT 1 FROM conflicts WHERE tender_id=? AND evaluator=? AND (vendor_id IS NULL OR vendor_id IN (%s)) LIMIT 1" % placeholders,
                (tender["id"], actor, *sorted(related_vendor_ids)),
            ).fetchone()
            if conflict:
                raise DomainError("评审人与联合体成员存在利益冲突" if bid["consortium_id"] else "评审人与该供应商存在利益冲突", 403)
            criteria = json.loads(tender["criteria"])
            missing = [c["name"] for c in criteria if c["name"] not in values]
            if missing:
                raise DomainError("缺少评分项: " + ",".join(missing))
            created = []
            now = utcnow()
            for criterion in criteria:
                try:
                    raw = float(values[criterion["name"]])
                except (TypeError, ValueError) as exc:
                    raise DomainError("评分值必须是数值") from exc
                if raw < 0 or raw > criterion["max_value"]:
                    raise DomainError("评分值超出范围: " + criterion["name"])
                if criterion["kind"] == "direct":
                    score = raw / criterion["max_value"] * 100
                else:
                    benchmark = criterion["max_value"]
                    score = min(100.0, benchmark / raw * 100) if raw > 0 else 0.0
                existing = conn.execute(
                    """SELECT * FROM evaluations WHERE bid_id=? AND evaluation_round=? AND evaluator=? AND criterion=?""",
                    (bid_id, tender["evaluation_round"], actor, criterion["name"]),
                ).fetchone()
                if existing:
                    raise DomainError("该评分项已提交，不能覆盖", 409)
                cur = conn.execute(
                    """INSERT INTO evaluations(bid_id,evaluation_round,evaluator,criterion,raw_value,score,comment,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (bid_id, tender["evaluation_round"], actor, criterion["name"], raw, score, comment.strip(), now, now),
                )
                created.append(dict(conn.execute("SELECT * FROM evaluations WHERE id=?", (cur.lastrowid,)).fetchone()))
            self._audit(conn, tender["id"], actor, "bid.evaluated", {"bid_id": bid_id, "criteria": [item["criterion"] for item in created]})
            return {"bid_id": bid_id, "evaluator": actor, "round": tender["evaluation_round"], "evaluations": created}

    def disqualify_bid(self, actor: str, role: str, bid_id: int, reason: str,
                       expected_version: int) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"procurement", "supervisor"}, "废标")
        if not reason.strip():
            raise DomainError("废标理由不能为空")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            bid = conn.execute("SELECT * FROM bids WHERE id=?", (bid_id,)).fetchone()
            if not bid:
                raise DomainError("投标不存在", 404)
            if bid["version"] != int(expected_version):
                raise DomainError("投标已变化，请刷新后重试", 409)
            if bid["status"] not in {"opened", "qualified"}:
                raise DomainError("当前投标不能废标", 409)
            conn.execute("UPDATE bids SET status='disqualified',version=version+1 WHERE id=?", (bid_id,))
            self._audit(conn, bid["tender_id"], actor, "bid.disqualified", {"bid_id": bid_id, "reason": reason.strip()})
            return dict(conn.execute("SELECT * FROM bids WHERE id=?", (bid_id,)).fetchone())

    def ask_clarification(self, actor: str, role: str, tender_id: int, vendor_id: int, question: str) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"vendor", "procurement", "supervisor"}, "提交澄清")
        if not question.strip():
            raise DomainError("澄清问题不能为空")
        with self.connect() as conn:
            self._tender(conn, tender_id)
            cur = conn.execute(
                "INSERT INTO clarifications(tender_id,vendor_id,question,created_at) VALUES(?,?,?,?)",
                (tender_id, vendor_id, question.strip(), utcnow()),
            )
            self._audit(conn, tender_id, actor, "clarification.asked", {"clarification_id": cur.lastrowid})
            return dict(conn.execute("SELECT * FROM clarifications WHERE id=?", (cur.lastrowid,)).fetchone())

    def answer_clarification(self, actor: str, role: str, clarification_id: int,
                             answer: str, publish: bool = True) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"procurement", "supervisor"}, "答复澄清")
        if not answer.strip():
            raise DomainError("澄清答复不能为空")
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM clarifications WHERE id=?", (clarification_id,)).fetchone()
            if not row:
                raise DomainError("澄清不存在", 404)
            if row["status"] != "pending":
                raise DomainError("澄清已经处理", 409)
            status = "published" if publish else "answered"
            conn.execute(
                "UPDATE clarifications SET answer=?,status=?,answered_by=?,answered_at=? WHERE id=?",
                (answer.strip(), status, actor, utcnow(), clarification_id),
            )
            self._audit(conn, row["tender_id"], actor, "clarification.answered", {"clarification_id": clarification_id, "published": publish})
            return dict(conn.execute("SELECT * FROM clarifications WHERE id=?", (clarification_id,)).fetchone())

    def submit_complaint(self, actor: str, role: str, tender_id: int, body: str) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"vendor", "evaluator", "procurement", "supervisor"}, "提交投诉")
        if not body.strip():
            raise DomainError("投诉内容不能为空")
        with self.connect() as conn:
            tender = self._tender(conn, tender_id)
            if tender["status"] in {"awarded", "cancelled"}:
                raise DomainError("项目已经结束，不能提交投诉", 409)
            cur = conn.execute(
                "INSERT INTO complaints(tender_id,complainant,body,created_at) VALUES(?,?,?,?)",
                (tender_id, actor, body.strip(), utcnow()),
            )
            self._audit(conn, tender_id, actor, "complaint.submitted", {"complaint_id": cur.lastrowid})
            return dict(conn.execute("SELECT * FROM complaints WHERE id=?", (cur.lastrowid,)).fetchone())

    def resolve_complaint(self, actor: str, role: str, complaint_id: int, decision: str,
                          resolution: str) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"supervisor"}, "处理投诉")
        if decision not in {"accepted", "rejected"} or not resolution.strip():
            raise DomainError("投诉决定或处理说明无效")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            complaint = conn.execute("SELECT * FROM complaints WHERE id=?", (complaint_id,)).fetchone()
            if not complaint:
                raise DomainError("投诉不存在", 404)
            if complaint["status"] != "open":
                raise DomainError("投诉已经处理", 409)
            conn.execute(
                "UPDATE complaints SET status=?,resolution=?,reviewed_by=?,resolved_at=? WHERE id=?",
                (decision, resolution.strip(), actor, utcnow(), complaint_id),
            )
            if decision == "accepted":
                tender = self._tender(conn, complaint["tender_id"])
                if tender["status"] in {"awarded", "cancelled"}:
                    raise DomainError("已结束项目不能重新评审", 409)
                conn.execute(
                    "UPDATE tenders SET status='reevaluation',evaluation_round=evaluation_round+1,evaluations_locked=0,version=version+1,updated_at=? WHERE id=?",
                    (utcnow(), tender["id"]),
                )
            self._audit(conn, complaint["tender_id"], actor, "complaint.resolved", {"complaint_id": complaint_id, "decision": decision})
            return dict(conn.execute("SELECT * FROM complaints WHERE id=?", (complaint_id,)).fetchone())

    def award_tender(self, actor: str, role: str, tender_id: int, expected_version: int) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"supervisor"}, "授标")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            tender = self._tender(conn, tender_id)
            if tender["status"] not in {"opened", "reevaluation"}:
                raise DomainError("当前项目不能授标", 409)
            if tender["version"] != int(expected_version):
                raise DomainError("项目已变化，请刷新后重试", 409)
            open_complaint = conn.execute("SELECT COUNT(*) AS c FROM complaints WHERE tender_id=? AND status='open'", (tender_id,)).fetchone()["c"]
            if open_complaint:
                raise DomainError("存在未处理投诉，不能授标", 409)
            all_open_bids = conn.execute("SELECT * FROM bids WHERE tender_id=? AND status IN ('opened','qualified')", (tender_id,)).fetchall()
            now_dt = datetime.now(timezone.utc)
            bids = []
            blocked_consortium_bids = []
            for bid in all_open_bids:
                if bid["consortium_id"]:
                    effective = self._consortium_effective(conn, bid["consortium_id"], now_dt)
                    if not effective["effective"]:
                        blocked_consortium_bids.append(
                            {"bid_id": bid["id"], "vendor_id": bid["vendor_id"],
                             "consortium_id": bid["consortium_id"], "reason": effective["reason"]}
                        )
                        continue
                bids.append(bid)
            criteria = json.loads(tender["criteria"])
            expected_criteria = {c["name"] for c in criteria}
            ranking = []
            for bid in bids:
                rows = conn.execute(
                    "SELECT criterion,AVG(score) AS score FROM evaluations WHERE bid_id=? AND evaluation_round=? GROUP BY criterion",
                    (bid["id"], tender["evaluation_round"]),
                ).fetchall()
                scores = {row["criterion"]: row["score"] for row in rows}
                if set(scores) != expected_criteria:
                    raise DomainError("投标尚未完成全部评分: %s" % bid["id"], 409)
                weighted = 0.0
                for criterion in criteria:
                    weighted += scores[criterion["name"]] * criterion["weight"] / 100
                entry = {"bid_id": bid["id"], "vendor_id": bid["vendor_id"], "price": bid["price"], "score": round(weighted, 2)}
                if bid["consortium_id"]:
                    entry["consortium_id"] = bid["consortium_id"]
                ranking.append(entry)
            if not ranking:
                raise DomainError("没有可授标的有效投标", 409)
            ranking.sort(key=lambda item: (-item["score"], item["price"], item["bid_id"]))
            winner = ranking[0]
            snapshot = {"tender_id": tender_id, "round": tender["evaluation_round"], "ranking": ranking,
                        "blocked_consortium_bids": blocked_consortium_bids,
                        "winner": winner, "awarded_by": actor, "awarded_at": utcnow()}
            conn.execute(
                "UPDATE tenders SET status='awarded',awarded_bid_id=?,award_snapshot=?,evaluations_locked=1,version=version+1,updated_at=? WHERE id=? AND version=?",
                (winner["bid_id"], json.dumps(snapshot, ensure_ascii=False), utcnow(), tender_id, expected_version),
            )
            conn.execute("UPDATE bids SET status='awarded',version=version+1 WHERE id=?", (winner["bid_id"],))
            self._audit(conn, tender_id, actor, "tender.awarded",
                        {"winner": winner, "ranking": ranking, "blocked_consortium_bids": blocked_consortium_bids})
            return {"tender": dict(self._tender(conn, tender_id)), "award": snapshot}

    def get_tender(self, actor: str, role: str, tender_id: int) -> dict[str, Any]:
        with self.connect() as conn:
            tender = dict(self._tender(conn, tender_id))
            bids = []
            if role in {"procurement", "supervisor", "auditor"} and tender["status"] in {"opened", "reevaluation", "awarded"}:
                bids = [dict(r) for r in conn.execute("SELECT * FROM bids WHERE tender_id=? ORDER BY id", (tender_id,)).fetchall()]
            elif role == "vendor":
                bids = []
                for row in conn.execute(
                    "SELECT b.*,t.status AS tender_status FROM bids b JOIN tenders t ON t.id=b.tender_id WHERE b.tender_id=? AND b.submitted_by=?",
                    (tender_id, actor),
                ).fetchall():
                    item = dict(row)
                    item.pop("tender_status", None)
                    if tender["status"] not in {"opened", "reevaluation", "awarded"}:
                        item.pop("payload", None)
                    bids.append(item)
            else:
                bids = [dict(r) for r in conn.execute(
                    "SELECT id,tender_id,vendor_id,consortium_id,price,status,payload_hash,submitted_at,opened_at FROM bids WHERE tender_id=? ORDER BY id",
                    (tender_id,),
                ).fetchall()]
            clarifications = [dict(r) for r in conn.execute(
                "SELECT id,tender_id,vendor_id,question,answer,status,answered_at FROM clarifications WHERE tender_id=? AND status='published' ORDER BY id",
                (tender_id,),
            ).fetchall()]
            self._attach_consortium_summaries(conn, bids)
            return {"tender": tender, "bids": bids, "clarifications": clarifications}

    def state(self, actor: str = "", role: str = "public") -> dict[str, Any]:
        with self.connect() as conn:
            now = datetime.now(timezone.utc)
            tenders = [dict(r) for r in conn.execute(
                "SELECT id,tender_no,title,description,status,deadline,evaluation_round,version,awarded_bid_id,created_at,updated_at FROM tenders ORDER BY id DESC"
            ).fetchall()]
            timeline = [dict(r) for r in conn.execute("SELECT * FROM timeline ORDER BY id DESC LIMIT 200").fetchall()]
            if role in {"procurement", "supervisor", "auditor"}:
                bids = [dict(r) for r in conn.execute(
                    """SELECT b.id,b.tender_id,b.vendor_id,b.consortium_id,b.price,b.status,b.payload_hash,b.submitted_at,b.opened_at,
                              CASE WHEN t.status IN ('opened','reevaluation','awarded') THEN b.payload ELSE NULL END AS payload
                       FROM bids b JOIN tenders t ON t.id=b.tender_id ORDER BY b.id DESC LIMIT 200"""
                ).fetchall()]
                complaints = [dict(r) for r in conn.execute("SELECT * FROM complaints ORDER BY id DESC LIMIT 100").fetchall()]
                consortia = [self._serialize_consortium(conn, r, now)
                             for r in conn.execute("SELECT * FROM consortia ORDER BY id DESC LIMIT 100").fetchall()]
            elif role == "vendor":
                bids = []
                for row in conn.execute(
                    """SELECT b.*,t.status AS tender_status FROM bids b JOIN tenders t ON t.id=b.tender_id
                       WHERE b.submitted_by=? ORDER BY b.id DESC LIMIT 100""",
                    (actor,),
                ).fetchall():
                    item = dict(row)
                    status = item.pop("tender_status")
                    if status not in {"opened", "reevaluation", "awarded"}:
                        item.pop("payload", None)
                    bids.append(item)
                complaints = [dict(r) for r in conn.execute(
                    "SELECT * FROM complaints WHERE complainant=? ORDER BY id DESC LIMIT 100", (actor,)
                ).fetchall()]
                vendor = conn.execute("SELECT id FROM vendors WHERE vendor_no=?", (actor,)).fetchone()
                consortia = []
                if vendor:
                    ids = [r["consortium_id"] for r in conn.execute(
                        "SELECT DISTINCT consortium_id FROM consortium_members WHERE vendor_id=?", (vendor["id"],)
                    ).fetchall()]
                    for cid in ids:
                        consortia.append(self._serialize_consortium(conn, self._consortium_row(conn, cid), now))
            else:
                bids, complaints, consortia = [], [], []
            self._attach_consortium_summaries(conn, bids)
        return {"tenders": tenders, "bids": bids, "complaints": complaints,
                "consortia": consortia, "timeline": timeline, "role": role}

    def seed_demo(self) -> dict[str, Any]:
        with self.connect() as conn:
            if conn.execute("SELECT COUNT(*) AS c FROM tenders").fetchone()["c"]:
                return {"seeded": False, "reason": "已有数据"}
        vendor = self.create_vendor("proc-demo", "procurement", "V-001", "启明科技", "vendor-demo")
        partner = self.create_vendor("proc-demo", "procurement", "V-002", "远山系统", "vendor-partner")
        deadline = (datetime.now(timezone.utc) + __import__("datetime").timedelta(hours=1)).isoformat(timespec="seconds")
        tender = self.create_tender(
            "proc-demo", "procurement", "TENDER-DEMO", "服务器采购", deadline,
            [{"name": "价格", "weight": 60, "kind": "cost", "max_value": 1000000},
             {"name": "质量", "weight": 40, "kind": "direct", "max_value": 100}],
        )
        published = self.publish_tender("proc-demo", "procurement", tender["id"], tender["version"])
        self.submit_bid("vendor-demo", "vendor", tender["id"], vendor["id"], {"价格": 900000, "质量": 90}, 900000)
        self.register_qualification("proc-demo", "procurement", vendor["id"], "CERT-001", "信息系统集成",
                                    (datetime.now(timezone.utc) + __import__("datetime").timedelta(days=365)).isoformat())
        self.register_qualification("proc-demo", "procurement", partner["id"], "CERT-002", "安防工程",
                                    (datetime.now(timezone.utc) + __import__("datetime").timedelta(days=365)).isoformat())
        consortium = self.register_consortium(
            "vendor-partner", "vendor", "CONS-DEMO", partner["id"],
            [{"vendor_id": partner["id"], "share": 60}, {"vendor_id": vendor["id"], "share": 40}],
        )
        self.submit_bid("vendor-partner", "vendor", tender["id"], partner["id"], {"价格": 880000, "质量": 92}, 880000,
                        consortium_id=consortium["id"])
        return {"seeded": True, "tender_id": tender["id"], "vendor_id": vendor["id"],
                "partner_id": partner["id"], "consortium_id": consortium["id"],
                "published_version": published["version"]}


class ApiHandler(BaseHTTPRequestHandler):
    service: ProcurementService

    def _send(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _headers(self) -> tuple[str, str]:
        return self.headers.get("X-User", ""), self.headers.get("X-Role", "public")

    def _json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 2_000_000:
            raise DomainError("请求体过大", 413)
        if not length:
            return {}
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise DomainError("请求体不是有效 JSON") from exc
        if not isinstance(value, dict):
            raise DomainError("JSON 请求体必须是对象")
        return value

    def do_GET(self) -> None:
        try:
            path = urlparse(self.path).path
            if path in {"/", "/index.html"}:
                body = (ROOT / "static" / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            actor, role = self._headers()
            if path == "/health":
                self._send(200, {"status": "ok", "service": "public-procurement"})
            elif path == "/api/state":
                self._send(200, self.service.state(actor, role))
            elif path == "/api/consortia":
                query = urlparse(self.path).query
                consortium_no = None
                if query:
                    for pair in query.split("&"):
                        key, _, value = pair.partition("=")
                        if key == "consortium_no":
                            consortium_no = value
                self._send(200, self.service.list_consortia(actor, role, consortium_no))
            elif path.startswith("/api/consortia/"):
                self._send(200, self.service.get_consortium(actor, role, int(path.split("/")[3])))
            elif path.startswith("/api/tenders/"):
                self._send(200, self.service.get_tender(actor, role, int(path.split("/")[3])))
            else:
                self._send(404, {"error": "接口不存在"})
        except DomainError as exc:
            self._send(exc.status, {"error": str(exc)})
        except (ValueError, IndexError) as exc:
            self._send(400, {"error": str(exc)})

    def do_POST(self) -> None:
        try:
            path, data, (actor, role) = urlparse(self.path).path, self._json(), self._headers()
            if path == "/api/vendors":
                result = self.service.create_vendor(actor, role, **data)
            elif path == "/api/qualifications":
                result = self.service.register_qualification(actor, role, **data)
            elif path == "/api/qualifications/suspension":
                result = self.service.set_qualification_suspension(actor, role, **data)
            elif path == "/api/consortia":
                result = self.service.register_consortium(actor, role, **data)
            elif path == "/api/tenders":
                result = self.service.create_tender(actor, role, **data)
            elif path == "/api/tenders/publish":
                result = self.service.publish_tender(actor, role, **data)
            elif path == "/api/bids":
                result = self.service.submit_bid(actor, role, **data)
            elif path == "/api/bids/withdraw":
                result = self.service.withdraw_bid(actor, role, **data)
            elif path == "/api/tenders/open":
                result = self.service.open_bids(actor, role, **data)
            elif path == "/api/conflicts":
                result = self.service.declare_conflict(actor, role, **data)
            elif path == "/api/evaluations":
                result = self.service.evaluate_bid(actor, role, **data)
            elif path == "/api/bids/disqualify":
                result = self.service.disqualify_bid(actor, role, **data)
            elif path == "/api/clarifications":
                result = self.service.ask_clarification(actor, role, **data)
            elif path == "/api/clarifications/answer":
                result = self.service.answer_clarification(actor, role, **data)
            elif path == "/api/complaints":
                result = self.service.submit_complaint(actor, role, **data)
            elif path == "/api/complaints/resolve":
                result = self.service.resolve_complaint(actor, role, **data)
            elif path == "/api/tenders/award":
                result = self.service.award_tender(actor, role, **data)
            else:
                raise DomainError("接口不存在", 404)
            self._send(201, result)
        except DomainError as exc:
            self._send(exc.status, {"error": str(exc)})
        except (KeyError, TypeError, ValueError) as exc:
            self._send(400, {"error": "请求参数错误: %s" % exc})
        except Exception as exc:
            self._send(500, {"error": "服务器内部错误", "detail": str(exc)})

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def serve(service: ProcurementService, host: str, port: int) -> None:
    ApiHandler.service = service
    server = ThreadingHTTPServer((host, port), ApiHandler)
    print("Public procurement service listening on http://%s:%s" % (host, port))
    server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="公共采购密封投标服务")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8209)
    parser.add_argument("--init", action="store_true")
    parser.add_argument("--seed", action="store_true")
    args = parser.parse_args()
    service = ProcurementService(args.db)
    if args.init:
        print(json.dumps(service.seed_demo() if args.seed else {"initialized": True, "db": args.db}, ensure_ascii=False))
        return
    serve(service, args.host, args.port)


if __name__ == "__main__":
    main()
