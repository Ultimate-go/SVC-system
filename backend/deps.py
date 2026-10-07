"""FastAPI 依赖：拿配置、拿管理器、取当前用户、要求管理员。"""

from __future__ import annotations

from typing import Iterator

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .manager import StoreManager
from .models import AuditRow, UserRow
from .security import TokenError, decode_token

__all__ = [
    "get_settings",
    "get_manager",
    "get_db",
    "current_user",
    "current_token",
    "require_admin",
    "audit",
    "http_error",
]

_bearer = HTTPBearer(auto_error=False)


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_manager(request: Request) -> StoreManager:
    return request.app.state.manager


def get_db(request: Request) -> Iterator[Session]:
    with request.app.state.db.session() as db:
        yield db


def current_token(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str:
    """本次请求的**原始令牌串**。

    ★ 只给一件事用：拿它去 ``manager.session_key_of()`` 取这次登录解封出来的
      会话私钥。**不要**拿它做权限判断 —— 权限一律看 :func:`current_user`
      查库得到的身份。
    """
    if creds is None or not creds.credentials:
        return ""
    return creds.credentials


def current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
    mgr: StoreManager = Depends(get_manager),
) -> UserRow:
    if creds is None or not creds.credentials:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "缺少令牌，请先登录")
    try:
        claims = decode_token(creds.credentials, settings)
    except TokenError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc

    # ★ 这张令牌被作废了吗（安全审计 I1）：`logout` 之后它必须**真的**不能用，
    #   否则“退出登录”只挡住了解密，而上传/改块/删除照样能发。
    if mgr.is_token_revoked(claims):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "这张令牌已被作废（已退出登录），请重新登录",
        )

    username = claims.get("sub", "")
    user = db.execute(
        select(UserRow).where(UserRow.username == username)
    ).scalar_one_or_none()
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户不存在")
    if user.disabled:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "账号已被停用")
    return user


def require_admin(user: UserRow = Depends(current_user)) -> UserRow:
    if user.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "需要管理员权限")
    return user


def audit(
    db: Session,
    actor: str,
    action: str,
    target: str = "",
    *,
    ok: bool = True,
    detail: str = "",
) -> None:
    """记一条审计流水。**只记元数据，绝不记内容** —— 免得日志把明文泄出去。"""
    db.add(
        AuditRow(actor=actor, action=action, target=target, ok=ok, detail=detail[:2000])
    )
    db.commit()


def http_error(code: int, message: str) -> HTTPException:
    return HTTPException(code, message)
