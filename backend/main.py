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
from .routers import admin, auth, files, system, verify

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
            "论文 ASIACRYPT 2020 / eprint 2020/149 §5.2 + §8.2。\n\n"
            "**设计 B：全系统一条向量** —— 所有用户的所有文件追加到同一条向量，"
            "共用一份摘要 δ，因此一个证据可以横跨多个文件。\n\n"
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

    # 演示用：前端可能从 Vite dev server（5173）过来
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(auth.router)
    app.include_router(admin.router)
    app.include_router(files.router)
    app.include_router(verify.router)
    app.include_router(system.router)

    async def _unhandled(request: Request, exc: Exception):  # pragma: no cover
        # 不把内部细节泄给客户端；调试时靠 settings.debug 打开
        log.exception("未处理异常: %s %s", request.method, request.url.path)
        detail = f"{type(exc).__name__}: {exc}" if settings.debug else "服务器内部错误"
        return JSONResponse(status_code=500, content={"detail": detail})

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
