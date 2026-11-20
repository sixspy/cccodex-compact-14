"""合并前阈值闸：按台参数阈值核验计划碎件，落盘闸检凭证。

设计选定（二选一，不同开）：
- 走「闸检账册」：每次启动合并前在同一写事务内重新核验阈值，把结果写入
  gate_checks 账册，不合格碎件落库为「已拒绝」并带原因；合并只收合格件。
- 不另建快照表，避免账册与快照两套落点互相打架。
"""
from __future__ import annotations

import hashlib
import json

from app.db import get_db
from app.services.errors import ServiceError

THRESHOLD_KEY = "compact_threshold_kb"
CANDIDATE_STATUSES = ("已入计划", "已拒绝")


def read_threshold_kb(conn) -> int:
    """读取现行压实阈值，任何缺失/非法都挡合并。"""
    row = conn.execute(
        "SELECT value FROM lab_settings WHERE key=?", (THRESHOLD_KEY,)
    ).fetchone()
    if row is None:
        raise ServiceError("台参数缺少压实阈值，无法闸检，已阻止合并", 409)
    raw = str(row["value"] or "").strip()
    try:
        value = int(float(raw))
    except ValueError as exc:
        raise ServiceError(f"台参数压实阈值无效：{raw}", 409) from exc
    if value <= 0:
        raise ServiceError(f"台参数压实阈值须大于 0：{raw}", 409)
    return value


def inspect_candidates(conn, plan_id: int, threshold_kb: int) -> dict:
    """核验计划下可合并碎件；返回合格/被拒清单。"""
    rows = conn.execute(
        "SELECT f.* FROM plan_items i JOIN lake_files f ON f.id=i.file_id "
        "WHERE i.plan_id=? AND f.status IN (?, ?) ORDER BY f.id",
        (plan_id, *CANDIDATE_STATUSES),
    ).fetchall()
    threshold_bytes = threshold_kb * 1024
    qualified: list[dict] = []
    rejected: list[dict] = []
    for row in rows:
        item = dict(row)
        size = int(item["size_bytes"])
        if size >= threshold_bytes:
            qualified.append(item)
        else:
            size_kb = max(1, round(size / 1024))
            item["reason"] = f"低于压实阈值 {threshold_kb}KB（当前 {size_kb}KB），不予合并"
            rejected.append(item)
    return {
        "candidates": [dict(r) for r in rows],
        "qualified": qualified,
        "rejected": rejected,
        "threshold_kb": threshold_kb,
    }


def _fingerprint(plan_id: int, threshold_kb: int, candidates: list[dict]) -> str:
    payload = {
        "plan_id": plan_id,
        "threshold_kb": threshold_kb,
        "items": [
            {"id": c["id"], "size_bytes": c["size_bytes"], "status": c["status"]}
            for c in candidates
        ],
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def record_gate_check(conn, plan: dict, threshold_kb: int) -> dict:
    """核验并在同一事务内落盘闸检凭证，同时改写碎件状态与原因。"""
    plan_id = int(plan["id"])
    inspection = inspect_candidates(conn, plan_id, threshold_kb)
    rejected = inspection["rejected"]
    passed = len(rejected) == 0
    status = "通过" if passed else "拒绝"
    rejected_ids = [int(x["id"]) for x in rejected]
    rejected_detail = [
        {
            "file_id": int(x["id"]),
            "path": x["path"],
            "size_bytes": int(x["size_bytes"]),
            "reason": x["reason"],
        }
        for x in rejected
    ]
    fingerprint = _fingerprint(plan_id, threshold_kb, inspection["candidates"])
    if passed:
        message = (
            f"闸检通过：{len(inspection['candidates'])} 个碎件均不低于 {threshold_kb}KB"
        )
    else:
        message = (
            f"闸检拒绝 {len(rejected)} 个低于 {threshold_kb}KB 的碎件，"
            f"合并未启动"
        )
    cur = conn.execute(
        "INSERT INTO gate_checks(plan_id, threshold_kb, status, checked_count, "
        "rejected_count, rejected_ids, rejected_detail, fingerprint, message) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            plan_id,
            threshold_kb,
            status,
            len(inspection["candidates"]),
            len(rejected),
            json.dumps(rejected_ids, ensure_ascii=False),
            json.dumps(rejected_detail, ensure_ascii=False),
            fingerprint,
            message,
        ),
    )
    gate_id = int(cur.lastrowid)
    for item in inspection["qualified"]:
        conn.execute(
            "UPDATE lake_files SET status='已入计划', reject_reason='', "
            "gate_check_id=? WHERE id=?",
            (gate_id, item["id"]),
        )
    for item in rejected:
        conn.execute(
            "UPDATE lake_files SET status='已拒绝', reject_reason=?, "
            "gate_check_id=? WHERE id=?",
            (item["reason"], gate_id, item["id"]),
        )
    row = conn.execute(
        "SELECT * FROM gate_checks WHERE id=?", (gate_id,)
    ).fetchone()
    return _hydrate(row)


def assert_threshold_aligned(gate: dict, current_threshold_kb: int) -> None:
    """闸检凭证阈值必须与现行台参数一致，偏了就挡合并落库。"""
    recorded = int(gate.get("threshold_kb") or 0)
    if recorded != int(current_threshold_kb):
        raise ServiceError(
            f"闸检阈值 {recorded}KB 与现行台参数 {current_threshold_kb}KB 不一致，"
            f"已阻止合并落库",
            409,
            {"gate": gate, "current_threshold_kb": int(current_threshold_kb)},
        )


def _hydrate(row) -> dict:
    gate = dict(row)
    gate["rejected_ids"] = decode_detail(gate.get("rejected_ids"))
    gate["rejected_detail"] = decode_detail(gate.get("rejected_detail"))
    return gate


def decode_detail(raw) -> list:
    if isinstance(raw, list):
        return raw
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def latest_gate(plan_id: int, db_path: str | None = None) -> dict | None:
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM gate_checks WHERE plan_id=? ORDER BY id DESC LIMIT 1",
            (plan_id,),
        ).fetchone()
        return _hydrate(row) if row else None


def list_gate_checks(
    plan_id: int | None = None, db_path: str | None = None
) -> list[dict]:
    with get_db(db_path) as conn:
        if plan_id is not None:
            rows = conn.execute(
                "SELECT g.*, p.plan_code, p.target_table FROM gate_checks g "
                "JOIN compact_plans p ON p.id=g.plan_id "
                "WHERE g.plan_id=? ORDER BY g.id DESC",
                (plan_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT g.*, p.plan_code, p.target_table FROM gate_checks g "
                "JOIN compact_plans p ON p.id=g.plan_id ORDER BY g.id DESC"
            ).fetchall()
        return [_hydrate(r) for r in rows]
