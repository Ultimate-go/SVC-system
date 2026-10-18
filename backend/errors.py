"""统一的**接口错误**：一句话 + 一个稳定的错误码。

为什么要有这个东西
------------------

界面上的每一句提示，最终都得有人"拥有"它。此前的分工是：后端拼好一整句中文
放进 ``HTTPException(detail=...)``，前端原样弹出来。代价是**改一个字都要重启
后端**（进程里的字符串，改文件不生效）。这个模块把一句话拆成两半：

* ``message`` —— 后端自己拼的中文原句。给 ``curl`` / 脚本 / 日志看，
  ``detail`` 字段照旧是字符串，**形状一个字没变**（老调用方不受影响）；
* ``code`` —— 稳定的机器码（如 ``files.not_owner``）。界面按它去
  ``frontend/src/data/errors.js`` 取自己那一份措辞，于是**改界面文案不必重启后端**。

``params`` 只放**界面能把这句话重新拼出来**的那些值（人名、端口号、块数）。
放不进去的东西（库异常的原文、节点名单）就别挂 code —— 那种情况让前端回退用
后端的 ``message``，见下面"边界"。

响应形状
--------

::

    {"detail": "仅文件所有者可以改写该文件：…", "code": "files.not_owner_modify"}
    {"detail": "用户 alice 已存在", "code": "users.exists", "params": {"username": "alice"}}

★ ``detail`` **永远**是字符串（不因为挂了 code 就变成对象）：``readableDetail``
与视图层那 35 处 ``e.response.data.detail`` 直接把它摆到界面上，形状一变全废。

边界（哪些错误**不**挂 code）
-----------------------------

* **库异常的原文透传**（各路由里的 ``except Xxx as exc: raise … str(exc)``）：
  那些句子在 ``core/`` / ``svc/`` / ``vds/`` 里，内容是内部分解（"挑战未收齐，
  缺少下标 [3, 7]"）。要搬到前端就得为每一类再造一套结构化参数，收益极小。
  它们照旧走 ``detail``，前端查不到 code 就原样显示。
* **FastAPI 自己的 422 参数校验**：``detail`` 是数组，形状由框架定，
  归一逻辑在 ``frontend/src/api/client.js::readableDetail``。

新增一个码的三步
----------------

1. 这里不用登记 —— 码只在 raise 处写；
2. 前端 ``frontend/src/data/errors.js`` 加一条（措辞 + 参数模板）；
3. 跑 ``python scripts/check_error_codes.py``：它比对两边，缺一条就报错。
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

__all__ = ["ApiError", "api_error_response", "install_error_handler"]


class ApiError(Exception):
    """发给客户端的错误：状态码 + 错误码 + 后端原句 + 参数。

    :param status_code: HTTP 状态码。
    :param code: 稳定错误码（``<区域>.<事项>``，全小写下划线）。
    :param message: 后端自己拼的中文原句（成为响应里的 ``detail``）。
    :param params: 界面重拼这句话需要的值。**只放能安全公开的**：
        人名、端口号、数量、下标。不放内部对象、不放异常实例。
    """

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        **params: Any,
    ) -> None:
        super().__init__(message)
        if not code or "." not in code:
            raise ValueError(f"错误码要写成 <区域>.<事项>，收到 {code!r}")
        self.status_code = int(status_code)
        self.code = code
        self.message = str(message)
        self.params: dict[str, Any] = dict(params)


def api_error_response(exc: ApiError) -> JSONResponse:
    """把 :class:`ApiError` 收成响应体。

    ``params`` 为空时**不出现**这个键 —— 空字典会让前端以为"有参数但都是空的"。
    """
    body: dict[str, Any] = {"detail": exc.message, "code": exc.code}
    if exc.params:
        body["params"] = exc.params
    return JSONResponse(status_code=exc.status_code, content=body)


def install_error_handler(app: FastAPI) -> None:
    """注册处理器。

    必须注册在"最后兜底"那个 ``Exception`` 处理器之前也无妨：Starlette 按
    ``type(exc).__mro__`` 逐级找，具体类型优先于 ``Exception``（见
    ``backend/main.py`` 里 ``_unhandled`` 的说明）。
    """

    async def _handler(request: Request, exc: ApiError) -> JSONResponse:
        return api_error_response(exc)

    app.add_exception_handler(ApiError, _handler)
