"""稳定错误码。所有可预期失败都抛出 ArchiveError，附带稳定 code。"""


class ArchiveError(Exception):
    def __init__(self, code, message, status=1):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status

    def as_dict(self):
        return {"error": self.code, "message": self.message}


def require(condition, code, message, status=1):
    if not condition:
        raise ArchiveError(code, message, status)
