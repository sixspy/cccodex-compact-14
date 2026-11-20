"""合并前阈值闸：闸检账册查询（页面抽核与库表核对共用）。"""
from app.handlers.base import BaseHandler
from app.services import gate as gate_svc


class ApiGateChecks(BaseHandler):
    def get(self):
        self.require_user()
        plan_arg = self.get_argument("plan_id", default="") or ""
        plan_id = int(plan_arg) if plan_arg.isdigit() else None
        self.json({"items": gate_svc.list_gate_checks(plan_id)})
