class ServiceError(Exception):
    def __init__(self, message: str, status_code: int = 400, extra: dict | None = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.extra = extra or {}
        # 闸检凭证已落盘后报错时置 True，写事务不再回滚凭证。
        self.committed = False

    def payload(self) -> dict:
        data = {"error": self.message}
        data.update(self.extra)
        return data
