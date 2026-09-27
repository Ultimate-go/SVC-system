"""口令哈希与 JWT。

* **口令**：bcrypt。先做一次 SHA-256 再喂给 bcrypt —— bcrypt 只取前 72 字节，
  预哈希能顺手消掉这个上限与 ``\\x00`` 截断问题，是常见做法。
* **令牌**：PyJWT，HS256。载荷只放 ``sub``（用户名）、``role``、``exp``、``iat``。
  **绝不把私钥或任何密钥材料放进令牌** —— JWT 是**签名**的、不是加密的，
  载荷谁都能读，放进去等于把私钥贴在门口。解密能力走**服务端内存会话**
  （``manager.remember_key``），不走令牌。
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from .config import Settings
from .models import UserRow

__all__ = [
    "hash_password",
    "verify_password",
    "create_token",
    "decode_token",
    "TokenError",
    "token_payload",
]


class TokenError(Exception):
    """令牌无效 / 过期。"""


def _prehash(password: str) -> bytes:
    return hashlib.sha256(password.encode("utf-8")).digest()


def hash_password(password: str) -> str:
    if not password:
        raise ValueError("口令不能为空")
    return bcrypt.hashpw(_prehash(password), bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, pwd_hash: str) -> bool:
    if not password or not pwd_hash:
        return False
    try:
        return bcrypt.checkpw(_prehash(password), pwd_hash.encode("ascii"))
    except (ValueError, TypeError):
        return False


def token_payload(user: UserRow) -> dict:
    """令牌载荷。**不含任何密钥材料**（理由见模块头部）。"""
    return {"sub": user.username, "role": user.role}


def create_token(user: UserRow, settings: Settings) -> str:
    now = datetime.now(timezone.utc)
    claims = {
        **token_payload(user),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.token_ttl_minutes)).timestamp()),
    }
    return jwt.encode(claims, settings.secret_key, algorithm="HS256")


def decode_token(token: str, settings: Settings) -> dict:
    try:
        return jwt.decode(token, settings.secret_key, algorithms=["HS256"])
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("令牌已过期，请重新登录") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError("令牌无效") from exc
