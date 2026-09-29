"""认证路由：登录、登出、取当前用户。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.keywrap import KeyWrapIntegrityError
from core.timing import collect

from .. import metrics
from ..deps import audit, current_token, current_user, get_db, get_manager, get_settings
from ..manager import NotFound, StoreManager
from ..models import UserRow
from ..schemas import LoginIn
from ..security import create_token, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])

_bearer = HTTPBearer(auto_error=False)


def user_public(u: UserRow, *, session_key: bool | None = None) -> dict:
    """对外的用户视图。**不含 pwd_hash，也不含任何私钥材料**。

    ``pub_key`` 是**公钥**，本来就是公开量 —— 给界面显示指纹用。
    私钥（以及它的密文）一律不出现在任何响应里。

    :param session_key: **本次会话**能不能解密。

        .. important::

           它与 :attr:`has_key` 是**两件事**，不能混：

           * ``has_key`` 是**库级**的 —— 库里有没有密钥对。后端重启、
             或本人点了退出之后，它**照样是 True**，而那两件事都会让
             “这次会话”解不了密（私钥只在**登录那一刻**进后端内存）。
           * ``session_key`` 是**会话级**的。``None`` 表示调用方没查
             （不是“不能”）。

           界面必须按 ``session_key`` 切换锁图标与「解密」按钮，
           否则会出现“页面看着全正常、一点解密就让你重新登录”。
    """
    return {
        "id": u.id,
        "username": u.username,
        "role": u.role,
        "display_name": u.display_name or u.username,
        #: 库里有没有密钥对（没有的话这个账号上传不了、也解密不了）
        "has_key": bool(u.pub_key and u.sk_wrapped),
        #: **本次会话**能不能解密；``None`` = 调用方没查
        "session_key": session_key,
        "pub_key": u.pub_key,
        "disabled": u.disabled,
    }


@router.post("/login")
def login(
    body: LoginIn,
    db: Session = Depends(get_db),
    settings=Depends(get_settings),
    mgr: StoreManager = Depends(get_manager),
):
    user = db.execute(
        select(UserRow).where(UserRow.username == body.username)
    ).scalar_one_or_none()

    # 用户不存在与口令错误返回同一句话 —— 不给枚举用户名的机会
    if user is None or not verify_password(body.password, user.pwd_hash):
        audit(db, body.username, "login", ok=False, detail="用户名或口令错误")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户名或口令错误")
    if user.disabled:
        audit(db, body.username, "login", ok=False, detail="账号已停用")
        raise HTTPException(status.HTTP_403_FORBIDDEN, "账号已被停用")

    # ★★ 这一步是「只有自己才能解密」的落点。
    #
    #   口令刚刚验过了，就用它把 SM2 私钥从 ``users.sk_wrapped`` 里解封出来，
    #   挂到这次令牌上（**只放内存**）。
    #
    #   为什么非要这样做、而不是登录时只发个 JWT：
    #   私钥**从来没有**以明文进过库，整个系统里只有口令能把它解出来。
    #   于是“能不能解密”绑的是**知道口令**这件事，而不是“手上有张令牌” ——
    #   后者只要偷到令牌就能冒充，前者不行。
    try:
        with collect() as sw:
            sk = mgr.unseal_user_key(user.username, body.password)
    except KeyWrapIntegrityError as exc:
        # 口令刚刚用 bcrypt 验过是对的，却解不开私钥密文 ⇒ 两份记录不同步：
        # 口令哈希换过，但私钥密文还是用**旧口令**包的（改口令时重封那一步没落地）。
        #
        # ★★ 提示里**绝不能**叫人"删掉重建这个账号"：重建 = 生成新密钥对 =
        #    此人名下的文件**永久**解不开（块密钥是用旧公钥封的，方案上没有第二把钥匙）。
        #    而这个故障本身是**无损可修**的 —— 私钥好端端在库里，只是外面那层
        #    包裹的口令不对。正确做法：用旧口令解封出来，再用新口令重新封装。
        audit(db, user.username, "login", ok=False, detail="私钥解封失败")
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "口令对了，但私钥解不开 —— 口令与私钥的包裹不同步"
            "（改口令时重新封装那一步没落地）。\n"
            "  ⚠ 请不要删除重建这个账号：那会换掉密钥对，"
            "此人名下的文件将永久无法解密。\n"
            "  正确修法：用旧口令解封私钥，再用当前口令重新封装 —— "
            "私钥本身完好，文件一把都不会丢。",
        ) from exc
    except NotFound as exc:
        audit(db, user.username, "login", ok=False, detail="没有密钥对")
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"这个账号还没有密钥对（{exc}）—— 请管理员重建，或者跑 seed --reset",
        ) from exc

    token = create_token(user, settings)
    mgr.remember_key(token, user.username, sk)

    audit(db, user.username, "login", ok=True)
    metrics.record("login", sw.total_ms(), sw.rows())
    return {
        "token": token,
        "token_type": "bearer",
        "user": user_public(user, session_key=True),
        # ★ 登录里最贵的一步就是"用口令把私钥解封出来"（20 万次 PBKDF2，
        #   本机约 0.1 秒）。单独报出来，免得被当成"后端登录慢"。
        "timings": sw.payload(),
    }


@router.post("/logout")
def logout(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    mgr: StoreManager = Depends(get_manager),
    db: Session = Depends(get_db),
    user: UserRow = Depends(current_user),
):
    """把会话里的私钥丢掉。

    JWT 是**无状态**的（签出去就收不回来），所以"退出登录"真正能做的只有
    一件事：**把内存里那把私钥丢掉** —— 而那恰恰是能解密的那一样东西。
    丢掉之后同一个令牌再调解密，会得到"会话里没有私钥，请重新登录"。
    """
    if creds is not None and creds.credentials:
        mgr.forget_key(creds.credentials)
    audit(db, user.username, "logout", ok=True)
    # ★ 把“退出到底做了什么”如实告诉前端。
    #   这不是客套话：JWT 收不回来，所以“登出”**不等于令牌失效**。
    #   真正被丢掉的是内存里那把私钥（＝解密能力）。说清楚，
    #   免得让人以为“我退了，别人拿着我的令牌就用不了了”。
    return {
        "ok": True,
        "token_still_valid": True,
        "what_happened": "服务端丢掉了本次会话的私钥",
        "notice": (
            "已退出：本机令牌已清除，服务端也丢掉了这次的会话私钥。"
            "此后同一张令牌调解密会要求重新登录。"
            "注意 JWT 是无状态的 —— 已签发的令牌在过期前仍然能通过身份校验，"
            "只是「没有私钥就解不开任何文件」而已。"
        ),
    }


@router.get("/me")
def me(
    user: UserRow = Depends(current_user),
    token: str = Depends(current_token),
    mgr: StoreManager = Depends(get_manager),
):
    """当前用户 + **本次会话**能不能解密。

    ★ 每次刷新页面都会调这里，所以会话态必须是**查出来的**、不是缓存的：
      后端重启或本人退出之后，同一张令牌仍然能过（JWT 无状态），
      但私钥已经不在内存里了 —— 界面靠这个字段立刻把锁图标与「解密」
      按钮切过去，而不是让人点了才发现 403。
    """
    return user_public(user, session_key=mgr.session_key_of(token) is not None)
