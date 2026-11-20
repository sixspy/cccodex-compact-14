"""压实计划：创建、发布、挂文件。"""
from __future__ import annotations

from app.db import get_db
from app.services.errors import ServiceError


def list_plans(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT p.*, "
            "(SELECT COUNT(*) FROM plan_items i WHERE i.plan_id=p.id) AS item_count, "
            "(SELECT COALESCE(SUM(f.size_bytes),0) FROM plan_items i "
            " JOIN lake_files f ON f.id=i.file_id WHERE i.plan_id=p.id) AS plan_bytes, "
            "(SELECT COUNT(*) FROM plan_items i JOIN lake_files f ON f.id=i.file_id "
            " WHERE i.plan_id=p.id AND f.status='已拒绝') AS rejected_count, "
            "g.status AS gate_status, g.threshold_kb AS gate_threshold_kb, "
            "g.rejected_count AS gate_rejected_count, g.message AS gate_message, "
            "g.created_at AS gate_checked_at, g.id AS gate_check_id "
            "FROM compact_plans p LEFT JOIN gate_checks g ON g.id=("
            "  SELECT id FROM gate_checks WHERE plan_id=p.id ORDER BY id DESC LIMIT 1"
            ") ORDER BY p.id"
        ).fetchall()
        return [dict(r) for r in rows]


def create_plan(
    plan_code: str,
    title: str,
    target_table: str,
    note: str = "",
    db_path: str | None = None,
) -> dict:
    plan_code = (plan_code or "").strip()
    title = (title or "").strip()
    target_table = (target_table or "").strip()
    if not plan_code or not title or not target_table:
        raise ServiceError("计划编号、标题、目标表必填")
    with get_db(db_path) as conn:
        try:
            cur = conn.execute(
                "INSERT INTO compact_plans(plan_code, title, status, target_table, note) "
                "VALUES (?,?, '草稿', ?, ?)",
                (plan_code, title, target_table, note or ""),
            )
        except Exception as exc:
            raise ServiceError(f"创建计划失败：{exc}") from exc
        return dict(
            conn.execute(
                "SELECT * FROM compact_plans WHERE id=?", (cur.lastrowid,)
            ).fetchone()
        )


def attach_loose_files(plan_id: int, db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        plan = conn.execute(
            "SELECT * FROM compact_plans WHERE id=?", (plan_id,)
        ).fetchone()
        if not plan:
            raise ServiceError("计划不存在", 404)
        if plan["status"] not in ("草稿", "已发布"):
            raise ServiceError("当前计划状态不可挂文件", 409)
        # 已拒绝件重新挂入，留给合并前闸检按现行阈值重新核验。
        loose = conn.execute(
            "SELECT id FROM lake_files WHERE table_name=? AND status IN ('碎文件','已拒绝')",
            (plan["target_table"],),
        ).fetchall()
        attached = 0
        for row in loose:
            exists = conn.execute(
                "SELECT 1 FROM plan_items WHERE plan_id=? AND file_id=?",
                (plan_id, row["id"]),
            ).fetchone()
            if exists:
                conn.execute(
                    "UPDATE lake_files SET status='已入计划', reject_reason='' "
                    "WHERE id=? AND status='已拒绝'",
                    (row["id"],),
                )
                continue
            conn.execute(
                "INSERT INTO plan_items(plan_id, file_id) VALUES (?,?)",
                (plan_id, row["id"]),
            )
            conn.execute(
                "UPDATE lake_files SET status='已入计划', reject_reason='' WHERE id=?",
                (row["id"],),
            )
            attached += 1
        return {"plan_id": plan_id, "attached": attached}


def publish_plan(plan_id: int, db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        plan = conn.execute(
            "SELECT * FROM compact_plans WHERE id=?", (plan_id,)
        ).fetchone()
        if not plan:
            raise ServiceError("计划不存在", 404)
        if plan["status"] != "草稿":
            raise ServiceError("仅草稿可发布", 409)
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM plan_items WHERE plan_id=?", (plan_id,)
        ).fetchone()["c"]
        if n <= 0:
            raise ServiceError("空计划不可发布", 409)
        conn.execute(
            "UPDATE compact_plans SET status='已发布' WHERE id=?", (plan_id,)
        )
        return dict(
            conn.execute(
                "SELECT * FROM compact_plans WHERE id=?", (plan_id,)
            ).fetchone()
        )


def plan_detail(plan_id: int, db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        plan = conn.execute(
            "SELECT * FROM compact_plans WHERE id=?", (plan_id,)
        ).fetchone()
        if not plan:
            raise ServiceError("计划不存在", 404)
        items = conn.execute(
            "SELECT f.* FROM plan_items i JOIN lake_files f ON f.id=i.file_id "
            "WHERE i.plan_id=? ORDER BY f.id",
            (plan_id,),
        ).fetchall()
        return {"plan": dict(plan), "items": [dict(x) for x in items]}
