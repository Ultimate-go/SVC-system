"""FastAPI 应用入口。

跑法::

    python -m uvicorn backend.main:app --reload      # 开发
    python -m uvicorn backend.main:app               # 演示（不要加 --reload）

.. warning::

   **必须单进程单 worker。** 全局向量在内存里，多 worker 会各持一份并互相
   覆盖。演示规模下这是正确取舍 —— 想扩就得把向量状态挪到外部存储，
   而"存储层拆进程"本来就是规划的下一步。

启动时会 :meth:`~backend.manager.StoreManager.bootstrap`：库里有公开参数
就重建，没有才生成一次。**绝不重新生成 N、g** —— 那会让所有旧文件的验证失败。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import Settings, default_settings
from .db import Database
from .manager import StoreManager
from .routers import admin, auth, files, perf, system, verify

__all__ = ["create_app", "app"]

log = logging.getLogger("vds")


class Utf8JSONResponse(JSONResponse):
    """带 ``charset=utf-8`` 的 JSON 响应。

    JSON 本来就是 UTF-8，但不少老客户端（含 PowerShell 5.1 的
    ``Invoke-RestMethod``）**看到 ``application/json`` 不带 charset 就按
    Latin-1 解码**，于是中文全变乱码。显式写上去，省得以后误以为是服务端 bug。
    """

    media_type = "application/json; charset=utf-8"


@asynccontextmanager
async def _lifespan(app: FastAPI):
    app.state.manager.bootstrap()
    st = app.state.manager.status()
    log.info(
        "VDS 就绪：|N|=%s 位, l=%s, n_max=%s, 已有 %s 块 / %s 个文件",
        st["crs"]["N_bits"],
        st["crs"]["l"],
        st["crs"]["n_max"],
        st["blocks"],
        st["files"],
    )
    yield


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or default_settings()
    db = Database(settings.database_url(), echo=settings.debug)
    manager = StoreManager(settings, db)

    app = FastAPI(
        title="基于增量聚合向量承诺的可验证分布式存储与可控共享系统",
        description=(
            "论文 ASIACRYPT 2020 / eprint 2020/149 §5.2 + §8.2"
            "（会议版 §8 = 全文版 §7）。\n\n"
            "**一文件一向量 + 跳文件合并**：每份文件在全局素数表里占自己的一段"
            "（段永不回收，删文件只留下空洞），各有自己的摘要 δ = (U, C, n)；"
            "跳文件验证时按「合并位置集」重算一份证据 —— 所以一个证据仍然可以"
            "横跨多个文件。\n\n"
            "**验证不受限**（δ 公开，谁都能验任意位置）；"
            "**解密受限**（只有文件所有者解得开 —— 块密钥用他的 SM2 公钥封装，"
    "别人的私钥解不开，是**密码学强制**而不是一张表说了算）。"
        ),
        version="0.2.0",
        lifespan=_lifespan,
        default_response_class=Utf8JSONResponse,
    )
    app.state.settings = settings
    app.state.db = db
    app.state.manager = manager

    if settings.secret_key_is_temporary:
        # ★ 让它**可见**（安全审计 P3）：临时签名密钥**每次启动都换**，
        #   于是所有已签发的令牌立刻失效。单进程 demo 里这是设计如此 ——
        #   但必须能看见，否则用户只会看到“刚登录怎么又要登录”。
        log.warning(
            "未设置 VDS_SECRET_KEY —— 本次使用**临时**签名密钥，"
            "重启后所有已签发的令牌都会失效。"
            "（单进程 demo 可以接受；多进程 / 长期运行必须显式设置它。）"
        )

    # 前端从 Vite dev server 过来。
    #
    # ★ 以前是 ``allow_origins=["*"]``（安全审计 P2）：任意站点都能从受害者
    #   浏览器里调这套 API。危害有限（认证走 ``Authorization`` 头而不是 Cookie，
    #   所以攻击者站点拿不到令牌就发不出有效请求），但那是**非必要的暴露面** ——
    #   收成“只认本机”零成本。
    #
    #   用 `allow_origin_regex` 而不是写死端口：前端端口可配（管理员界面能改），
    #   写死会随端口一变就断。
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"^http://(127\.0\.0\.1|localhost)(:\d+)?$",
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ---- 请求体上限（安全审计 I4）----
    #
    # ★ 为什么要有它：`routers/files.py` 的 `MAX_UPLOAD_BYTES` 是在**处理函数
    #   里面**施加的 —— 那时请求体**已经被完整接收**（Starlette 的 multipart
    #   解析用 `SpooledTemporaryFile`，超阈值落磁盘）。也就是说，一个超大 body
    #   仍然会先把带宽和磁盘吃掉，才轮到那句判断。
    #
    #   这里按 `Content-Length` 在进入路由**之前**就拒掉。上限取 16 MiB：
    #   8 MiB 的上传内容 + multipart 边界与编码开销的余量。
    #
    # ★ 诚实边界：不信 `Content-Length`（chunked 上传）的客户端绕得过这道。
    #   真正硬的上限要落在反向代理或 uvicorn 的参数上；这里只是把最常见的
    #   那个洞堵上，并让超限变成一个明确的 413。
    max_request_bytes = 16 * 1024 * 1024

    @app.middleware("http")
    async def _limit_request_body(request: Request, call_next):
        raw = request.headers.get("content-length")
        if raw is not None:
            try:
                declared = int(raw)
            except ValueError:
                return JSONResponse(
                    status_code=400,
                    content={"detail": f"Content-Length 不是整数：{raw!r}"},
                )
            if declared > max_request_bytes:
                return JSONResponse(
                    status_code=413,
                    content={
                        "detail": (
                            f"请求体 {declared} 字节超过上限 "
                            f"{max_request_bytes // 1024 // 1024} MiB"
                        )
                    },
                )
        return await call_next(request)

    app.include_router(auth.router)
    app.include_router(admin.router)
    app.include_router(files.router)
    app.include_router(verify.router)
    app.include_router(system.router)
    app.include_router(perf.router)

    async def _unhandled(request: Request, exc: Exception):  # pragma: no cover
        # 不把内部细节泄给客户端；调试时靠 settings.debug 打开
        log.exception("未处理异常: %s %s", request.method, request.url.path)
        detail = f"{type(exc).__name__}: {exc}" if settings.debug else "服务器内部错误"
        return JSONResponse(status_code=500, content={"detail": detail})

    # ★★ 注册它（安全审计 P1）：这个函数以前**只被定义、从未注册** ——
    #   FastAPI 会用自带的默认处理器，于是注释里承诺的“不把内部细节泄给客户端”
    #   其实没有实现，``settings.debug`` 那个分支也永远不会生效。
    #
    #   用 ``Exception`` 做类型注册，语义是“最后兜底”：更具体的
    #   ``HTTPException`` / ``RequestValidationError`` 处理器优先级更高，
    #   不会受影响（那些是 4xx，不该变成 500）。
    app.add_exception_handler(Exception, _unhandled)

    @app.get("/health", tags=["system"])
    def health():
        """探活。**不需要登录，也不查库、不问节点。**

        它回答的是“这个进程还在不在”，不是“系统健不健康” —— 后者要看
        ``/api/status``（全局摘要）与 ``/api/nodes``（各台现状）。所以它故意
        不带任何状态：启动脚本 / 监控 / 容器探针要的是一个**永远很快**的答案。
        """
        return {"ok": True, "service": "vds-system"}

    @app.get("/", tags=["system"])
    def root():
        return {
            "name": "vds-system",
            "docs": "/docs",
            "health": "/health",
            "status": "/api/status",
            "ui": "前端另起（见 README 的「一键启动」，默认 http://127.0.0.1:5173）",
        }

    return app


app = create_app()


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run("backend.main:app", host="127.0.0.1", port=8000, reload=False)
