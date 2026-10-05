"""统一的业务错误类型与错误码。"""


class AppError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 422, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}


ERR_ORDER_NOT_FOUND = ("ORDER_NOT_FOUND", 404)
ERR_EVENT_ORDER_INVALID = ("EVENT_ORDER_INVALID", 422)
ERR_CORRECTION_TARGET_NOT_FOUND = ("CORRECTION_TARGET_NOT_FOUND", 422)
ERR_POLICY_NOT_FOUND = ("POLICY_NOT_FOUND", 422)
ERR_CALENDAR_NOT_FOUND = ("CALENDAR_NOT_FOUND", 422)
ERR_ALREADY_ARCHIVED = ("ALREADY_ARCHIVED", 409)
