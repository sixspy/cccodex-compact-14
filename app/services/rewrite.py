"""重写合并：按计划执行合并任务；启动前须过合并前阈值闸。"""
from __future__ import annotations

import sqlite3

from app.db import get_db, write_transaction
from app.services import gate as gate_svc
from app.services.errors import ServiceError


def list_jobs(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT j.*, p.plan_code, p.target_table, "
            "g.status AS gate_status, g.threshold_kb AS gate_threshold_kb, "
            "g.rejected_count AS gate_rejected_count, "
            "g.rejected_detail AS gate_rejected_detail FROM rewrite_jobs j "
            "JOIN compact_plans p ON p.id=j.plan_id "
            "LEFT JOIN gate_checks g ON g.id=j.gate_check_id "
            "ORDER BY j.id DESC"
        ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            detail = item.get("gate_rejected_detail")
            item["gate_rejected_detail"] = gate_svc.decode_detail(detail)
            out.append(item)
        return out


def start_rewrite(plan_id: int, db_path: str | None = None) -> dict:
    """启动合并。

    单次落盘闸检：在同一个 BEGIN IMMEDIATE 写事务里读阈值、核验碎件、
    落盘 gate_checks 凭证并执行合并。失败（含闸检拒绝/阈值不齐）回滚，
    不会产生半态任务；两个仓管同时点启动只会有一份闸检凭证。
    """
    try:
        return _start_rewrite_locked(plan_id, db_path)
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            raise ServiceError("另有仓管正在启闸检合并，请稍后重试", 409) from exc
        raise


def _start_rewrite_locked(plan_id: int, db_path: str | None = None) -> dict:
    with write_transaction(db_path) as conn:
        plan = conn.execute(
            "SELECT * FROM compact_plans WHERE id=?", (plan_id,)
        ).fetchone()
        if not plan:
            raise ServiceError("计划不存在", 404)
        if plan["status"] not in ("已发布", "执行中"):
            raise ServiceError("仅已发布计划可启动合并", 409)
        running = conn.execute(
            "SELECT id FROM rewrite_jobs WHERE plan_id=? AND status IN ('待执行','执行中')",
            (plan_id,),
        ).fetchone()
        if running:
            raise ServiceError("已有在途合并任务", 409)

        threshold_kb = gate_svc.read_threshold_kb(conn)
        gate = gate_svc.record_gate_check(conn, dict(plan), threshold_kb)
        if gate["status"] != "通过":
            # 闸检凭证与被拒件原因须落盘，合并本身不启动。
            # 合并队列留一条失败任务并挂闸检芯片，页面可直接抽核。
            conn.execute(
                "INSERT INTO rewrite_jobs(plan_id, status, merged_bytes, message, "
                "gate_check_id, started_at, finished_at) "
                "VALUES (?, '失败', 0, ?, ?, datetime('now'), datetime('now'))",
                (plan_id, gate["message"], gate["id"]),
            )
            conn.execute("COMMIT")
            blocked = ServiceError(
                gate["message"],
                409,
                {"gate": gate, "rejected_files": gate["rejected_detail"]},
            )
            blocked.committed = True
            raise blocked

        items = conn.execute(
            "SELECT f.* FROM plan_items i JOIN lake_files f ON f.id=i.file_id "
            "WHERE i.plan_id=? AND f.status='已入计划' ORDER BY f.id",
            (plan_id,),
        ).fetchall()
        if not items:
            raise ServiceError("计划下无可合并碎文件", 409)
        # 闸检后阈值仍须与凭证一致，偏了挡合并落库。
        current = gate_svc.read_threshold_kb(conn)
        gate_svc.assert_threshold_aligned(gate, current)

        total = sum(int(x["size_bytes"]) for x in items)
        cur = conn.execute(
            "INSERT INTO rewrite_jobs(plan_id, status, merged_bytes, message, "
            "gate_check_id, started_at) "
            "VALUES (?, '执行中', 0, '合并进行中', ?, datetime('now'))",
            (plan_id, gate["id"]),
        )
        job_id = cur.lastrowid
        conn.execute(
            "UPDATE compact_plans SET status='执行中' WHERE id=?", (plan_id,)
        )
        # baseline: synchronous success path (出题可改失败半态)
        for row in items:
            conn.execute(
                "UPDATE lake_files SET status='已合并' WHERE id=?", (row["id"],)
            )
        conn.execute(
            "UPDATE rewrite_jobs SET status='成功', merged_bytes=?, message=?, "
            "finished_at=datetime('now') WHERE id=?",
            (total, f"已合并 {len(items)} 个碎文件（闸检#{gate['id']}）", job_id),
        )
        conn.execute(
            "UPDATE compact_plans SET status='已完成' WHERE id=?", (plan_id,)
        )
        return dict(
            conn.execute(
                "SELECT * FROM rewrite_jobs WHERE id=?", (job_id,)
            ).fetchone()
        )


def fail_open_job(job_id: int, reason: str, db_path: str | None = None) -> dict:
    """预留：标记失败（基线测试与切口）。"""
    with get_db(db_path) as conn:
        job = conn.execute(
            "SELECT * FROM rewrite_jobs WHERE id=?", (job_id,)
        ).fetchone()
        if not job:
            raise ServiceError("任务不存在", 404)
        conn.execute(
            "UPDATE rewrite_jobs SET status='失败', message=?, finished_at=datetime('now') "
            "WHERE id=?",
            (reason or "合并失败", job_id),
        )
        return dict(
            conn.execute(
                "SELECT * FROM rewrite_jobs WHERE id=?", (job_id,)
            ).fetchone()
        )
