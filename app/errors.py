"""领域错误，路由层统一翻译成 HTTP 状态码。"""


class DomainError(Exception):
    status_code = 400


class ConflictError(DomainError):
    """同一轮对装车段重叠 / 业务唯一约束冲突。"""

    status_code = 409


class MissingBaselineError(DomainError):
    """找不到截至查询日的大修，缺少起算日。"""

    status_code = 422


class NotFoundError(DomainError):
    status_code = 404
