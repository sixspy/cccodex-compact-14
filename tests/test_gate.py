import json
import threading

import pytest

from app.db import connect, get_db
from app.services import files, gate, plans, rewrite, settings
from app.services.errors import ServiceError


def _add_file(db, path, table, size):
    return files.register_file(path, table, size, db_path=db)


def _plan_with_files(db, sizes, code="CP-GATE"):
    p = plans.create_plan(code, "闸检计划", "dw_gate", db_path=db)
    for i, size in enumerate(sizes, start=1):
        _add_file(db, f"lake/dw_gate/part-{i:04d}.parquet", "dw_gate", size)
    plans.attach_loose_files(p["id"], db_path=db)
    plans.publish_plan(p["id"], db_path=db)
    return p


def test_gate_passes_all_large_files(db):
    p = _plan_with_files(db, [300 * 1024, 512 * 1024])
    job = rewrite.start_rewrite(p["id"], db_path=db)
    assert job["status"] == "成功"
    assert job["gate_check_id"]
    gates = gate.list_gate_checks(p["id"], db_path=db)
    assert len(gates) == 1
    assert gates[0]["status"] == "通过"
    assert gates[0]["threshold_kb"] == 256
    assert gates[0]["rejected_count"] == 0


def test_gate_rejects_small_files_and_persists_reason(db):
    p = _plan_with_files(db, [300 * 1024, 10 * 1024, 20 * 1024])
    with pytest.raises(ServiceError) as exc:
        rewrite.start_rewrite(p["id"], db_path=db)
    assert exc.value.status_code == 409
    assert exc.value.committed is True

    # 闸检凭证落库（单次落盘）
    gates = gate.list_gate_checks(p["id"], db_path=db)
    assert len(gates) == 1
    g = gates[0]
    assert g["status"] == "拒绝"
    assert g["rejected_count"] == 2
    assert g["threshold_kb"] == 256

    # 被拒件在碎件列表可读到原因
    items = files.list_files("dw_gate", db_path=db)
    rejected = [f for f in items if f["status"] == "已拒绝"]
    assert len(rejected) == 2
    for f in rejected:
        assert "低于压实阈值 256KB" in f["reject_reason"]
        assert f["gate_check_id"] == g["id"]
    # 合格件仍是已入计划，未被合并
    assert all(f["status"] != "已合并" for f in items)

    # 合并队列亮闸检芯片
    jobs = rewrite.list_jobs(db_path=db)
    assert len(jobs) == 1
    assert jobs[0]["status"] == "失败"
    assert jobs[0]["gate_status"] == "拒绝"
    assert jobs[0]["gate_check_id"] == g["id"]
    assert len(jobs[0]["gate_rejected_detail"]) == 2


def test_rejected_file_is_not_swallowed_after_threshold_change(db):
    p = _plan_with_files(db, [300 * 1024, 10 * 1024])
    with pytest.raises(ServiceError):
        rewrite.start_rewrite(p["id"], db_path=db)
    # 调低阈值后重挂重跑，原被拒件应被重新核验并合并
    settings.update_setting("compact_threshold_kb", "8", db_path=db)
    plans.attach_loose_files(p["id"], db_path=db)
    job = rewrite.start_rewrite(p["id"], db_path=db)
    assert job["status"] == "成功"
    merged = [f for f in files.list_files("dw_gate", db_path=db) if f["status"] == "已合并"]
    assert len(merged) == 2
    assert all(f["reject_reason"] == "" for f in merged)


def test_gate_threshold_must_align_with_current_setting(db):
    p = _plan_with_files(db, [300 * 1024])
    g = gate.latest_gate(p["id"], db_path=db)
    # 还没有凭证
    assert g is None
    with get_db(db) as conn:
        threshold = gate.read_threshold_kb(conn)
        plan = conn.execute("SELECT * FROM compact_plans WHERE id=?", (p["id"],)).fetchone()
        recorded = gate.record_gate_check(conn, dict(plan), threshold)
    gate.assert_threshold_aligned(recorded, threshold)
    with pytest.raises(ServiceError):
        gate.assert_threshold_aligned(recorded, threshold + 64)


def test_threshold_invalid_blocks_merge(db):
    p = _plan_with_files(db, [300 * 1024])
    with pytest.raises(ServiceError):
        settings.update_setting("compact_threshold_kb", "abc", db_path=db)
    with get_db(db) as conn:
        conn.execute("UPDATE lab_settings SET value='0' WHERE key='compact_threshold_kb'")
    with pytest.raises(ServiceError) as exc:
        rewrite.start_rewrite(p["id"], db_path=db)
    assert exc.value.status_code == 409
    # 阈值非法时也不应落闸检凭证（无法核验）
    assert gate.list_gate_checks(p["id"], db_path=db) == []


def test_concurrent_gate_writes_single_voucher(db):
    p = _plan_with_files(db, [300 * 1024, 512 * 1024])
    results = []

    def worker():
        try:
            results.append(("ok", rewrite.start_rewrite(p["id"], db_path=db)["status"]))
        except ServiceError as exc:
            results.append(("err", exc.message))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(r[0] for r in results) == ["err", "ok"]
    gates = gate.list_gate_checks(p["id"], db_path=db)
    assert len(gates) == 1
    jobs = rewrite.list_jobs(db_path=db)
    assert len(jobs) == 1


def test_gate_voucher_survives_db_reopen(db):
    p = _plan_with_files(db, [10 * 1024])
    with pytest.raises(ServiceError):
        rewrite.start_rewrite(p["id"], db_path=db)
    conn = connect(db)
    row = conn.execute("SELECT * FROM gate_checks WHERE plan_id=?", (p["id"],)).fetchone()
    conn.close()
    assert row is not None
    detail = json.loads(row["rejected_detail"])
    assert detail and detail[0]["file_id"]
    assert "低于压实阈值" in detail[0]["reason"]
