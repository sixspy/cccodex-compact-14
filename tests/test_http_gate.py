import json
import os
import tempfile
import unittest

from tornado.testing import AsyncHTTPTestCase

from app.main import make_app
from app.seed import reset_seed


class GateHttpTest(AsyncHTTPTestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._db = os.path.join(self._tmpdir.name, "http.db")
        self._old = os.environ.get("DATABASE_PATH")
        os.environ["DATABASE_PATH"] = self._db
        reset_seed(self._db)
        self._cookie = None
        super().setUp()

    def tearDown(self):
        super().tearDown()
        if self._old is None:
            os.environ.pop("DATABASE_PATH", None)
        else:
            os.environ["DATABASE_PATH"] = self._old
        self._tmpdir.cleanup()

    def get_app(self):
        return make_app()

    def _headers(self):
        headers = {"Content-Type": "application/json"}
        if self._cookie:
            headers["Cookie"] = self._cookie
        return headers

    def _post(self, path, body=None):
        return self.fetch(
            path,
            method="POST",
            body=json.dumps(body if body is not None else {}),
            headers=self._headers(),
            raise_error=False,
        )

    def _get(self, path):
        return self.fetch(path, headers=self._headers(), raise_error=False)

    def _login(self):
        r = self._post("/api/login", {"username": "admin", "password": "123456"})
        assert r.code == 200
        self._cookie = r.headers["Set-Cookie"].split(";")[0]

    def test_login_required(self):
        r = self._get("/api/gate/checks")
        assert r.code == 401

    def test_full_gate_flow_over_http(self):
        self._login()
        r = self._post(
            "/api/files",
            {"path": "lake/dw_users/tiny.parquet", "table_name": "dw_users", "size_bytes": 5 * 1024},
        )
        assert r.code == 200

        r = self._post(
            "/api/plans",
            {"plan_code": "CP-HTTP", "title": "HTTP闸检", "target_table": "dw_users"},
        )
        plan_id = json.loads(r.body)["item"]["id"]
        assert self._post(f"/api/plans/{plan_id}/attach").code == 200
        assert self._post(f"/api/plans/{plan_id}/publish").code == 200

        # 闸检拒绝：返回 409，凭证与被拒件已落盘
        r = self._post(f"/api/rewrite/plans/{plan_id}/start")
        assert r.code == 409
        payload = json.loads(r.body)
        assert payload["gate"]["status"] == "拒绝"
        assert payload["gate"]["threshold_kb"] == 256

        # 页面抽核：合并队列读到闸检芯片与被拒件
        jobs = json.loads(self._get("/api/rewrite/jobs").body)["items"]
        assert jobs[0]["status"] == "失败"
        assert jobs[0]["gate_status"] == "拒绝"
        assert jobs[0]["gate_rejected_count"] >= 1
        assert jobs[0]["gate_rejected_detail"][0]["file_id"]

        # 页面抽核：碎件列表读到拒绝原因（种子 dw_users 三件 180–210KB 加 tiny.parquet 均低于 256KB）
        items = json.loads(self._get("/api/files?table_name=dw_users").body)["items"]
        rejected = [f for f in items if f["status"] == "已拒绝"]
        assert len(rejected) == 4
        assert any(f["path"].endswith("tiny.parquet") for f in rejected)
        assert all("低于压实阈值" in f["reject_reason"] for f in rejected)
        assert all(f["status"] != "已合并" for f in items)

        # 库里可查闸检账册
        gates = json.loads(self._get("/api/gate/checks").body)["items"]
        assert len(gates) == 1
        assert gates[0]["plan_id"] == plan_id
        assert gates[0]["rejected_detail"][0]["reason"]


if __name__ == "__main__":
    unittest.main()
