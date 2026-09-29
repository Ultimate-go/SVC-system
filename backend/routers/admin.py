"""管理员路由：增删用户、看审计流水、触发全量自检。

**只有 ``admin`` 角色能进**（``Depends(require_admin)``）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .. import metrics
from ..deps import audit, current_token, current_user, get_db, get_manager, require_admin
from ..manager import StoreManager
from ..models import AuditRow, BlockRow, FileRow, UserRow
from ..schemas import UserCreateIn, UserPatchIn
from ..security import hash_password
from core.timing import collect
from .auth import user_public

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get("/users")
def list_users(_: UserRow = Depends(require_admin), db: Session = Depends(get_db)):
    rows = list(db.execute(select(UserRow).order_by(UserRow.id)).scalars())
    # ★ 带上“每人有几份文件”。删号对话框要在**删之前**就把后果说清楚 ——
    #   那些文件此后谁也解不开（方案上没有第二把钥匙），
    #   不能等删完了再告诉人家。
    counts = dict(
        db.execute(select(FileRow.owner, func.count()).group_by(FileRow.owner)).all()
    )
    return [{**user_public(u), "files": counts.get(u.username, 0)} for u in rows]


@router.post("/users", status_code=status.HTTP_201_CREATED)
def create_user(
    body: UserCreateIn,
    admin: UserRow = Depends(require_admin),
    mgr: StoreManager = Depends(get_manager),
    db: Session = Depends(get_db),
):
    exists = db.execute(
        select(UserRow).where(UserRow.username == body.username)
    ).scalar_one_or_none()
    if exists is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"用户 {body.username} 已存在")

    # ★ 已删除过的用户名**可以**复用 —— 但旧数据必须先改挂到墓碑名下。
    #
    #   那是 ``delete_user`` 那边的责任（见那里的注释）：删号时把
    #   ``files.owner`` / ``blocks.owner`` 一起改成 ``ghost（已删号#6）``
    #   这样的墓碑名。于是这里的重名检查只看 ``users`` 表就够 ——
    #   同名的新人会拿到一对**全新的**密钥，而旧文件挂在墓碑名下、
    #   与他不同名，``can_decrypt`` 于是如实报 false。
    #
    #   ★ 别把这里改回"查审计表禁止复用"：那样同样能挡住"列表说能解、
    #     点了 403"，但代价是用户名永久报废。改名方案两件事都做到了
    #     （还顺带让旧文件的所有者一眼可辨）。
    user = UserRow(
        username=body.username,
        pwd_hash=hash_password(body.password),
        role=body.role,
        display_name=body.display_name or body.username,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    # ★ 建用户的同时就把钥匙给他：生成一对 SM2 密钥，**私钥用他刚设的口令包好**
    #   存进 users.sk_wrapped。不生成的话他上传不了（没有公钥封块密钥），
    #   登录也解不了密。
    #
    #   注意：这里的口令**只在这一瞬间用到**（用来包私钥），不会被存起来 ——
    #   库里只有它的 bcrypt 哈希，那个是登录校验用的，推不回口令。
    with collect() as sw:
        mgr.ensure_user_key(user.username, body.password)
    # ★ ``ensure_user_key`` 是在**另一个 session** 里写的，这里的 ORM 对象
    #   已经过期 —— 不 refresh 的话响应里的 ``pub_key`` / ``has_key`` 还是旧值
    #   （表现为“建完用户立刻看，说他没有密钥对”）。
    db.refresh(user)
    audit(db, admin.username, "user_create", body.username, detail=f"role={body.role}")
    out = user_public(user)
    # ★ 阶段耗时：生成 SM2 密钥对 + 用刚设的口令封装私钥（20 万次 PBKDF2）
    out["timings"] = sw.payload()
    metrics.record("create_user", sw.total_ms(), sw.rows())
    return out


@router.patch("/users/{user_id}")
def patch_user(
    user_id: int,
    body: UserPatchIn,
    actor: UserRow = Depends(current_user),
    token: str = Depends(current_token),
    mgr: StoreManager = Depends(get_manager),
    db: Session = Depends(get_db),
):
    """改一个用户。

    ★★ 这里**不再一律要求管理员**，而是分两种情形：

      * **改自己**（只动 ``password`` / ``display_name``）—— 任何登录用户都能做。
        必须放行：改口令要重新封装自己的私钥，而那把私钥只有在**他自己登录**
        的那一刻才被解封出来（它从不落盘、只在内存里）。挡在 ``require_admin``
        后面的话，普通用户在「个人中心」改口令会永远 403。
      * **改别人**，或者动 ``role`` / ``disabled`` —— 仍然必须管理员。
        否则任何人都能把自已提权成 admin。

    ★★ 改口令的顺序（真踩过的坑，别再改回去）：
        **先把新私钥密文写进当前事务，再落新哈希，最后一起提交。**
        反过来（先提交哈希、再重封）的后果是：重封一失败就留下
        「哈希是新的、私钥还是旧口令包的」——那个账号**两个口令都登不进去**
        （新口令解不开私钥、旧口令过不了哈希），而私钥其实完好无损。
    """
    user = db.get(UserRow, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "用户不存在")

    is_self = user.id == actor.id
    is_admin = actor.role == "admin"
    #: 动这两项属于特权操作 —— 给自己提权/解停用不能靠"改自己"这条口子。
    privileged = body.role is not None or body.disabled is not None

    if not is_admin and (not is_self or privileged):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "需要管理员权限"
            if not is_self
            else "只有管理员能改角色或停用状态（改口令与显示名可以自己改）",
        )

    if body.display_name is not None:
        user.display_name = body.display_name
    if body.role is not None:
        user.role = body.role
    if body.disabled is not None:
        if is_self and body.disabled:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "不能停用自己")
        user.disabled = body.disabled

    if body.password:
        # ★★ 改口令**不能只改 pwd_hash**：私钥是用口令包着存的。
        #
        #   只改哈希的后果是：他下次登录时口令对得上，却**解不开自己的私钥**
        #   —— 于是他自己以前传的文件全部读不了，而且报错看上去像密钥坏了。
        #   所以改口令必须**重新封装私钥**。
        #
        #   重封需要"现在的私钥"，而后端只在**这个人登录那一刻**持有过它。
        #   所以：
        #     * 自己改自己的 —— 本次会话里就有他的私钥，没问题；
        #     * 管理员改别人的 —— 拿不到那人的私钥，**只能拒绝**。
        #       这不是偷懒：真做了就是把他的数据永久锁死。
        if not is_self:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"不能替 {user.username} 改口令：他的私钥是用他自己的口令包的，"
                f"改了他的口令就把他手里的私钥永久弄丢（他以前的文件会全部解不开）。\n"
                f"  让他自己登录后改（管理员可以在「用户管理」里停用他）。",
            )
        sk = mgr.session_key_of(token)
        if sk is None:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "这次会话里没有你的私钥，无法重封 —— 请重新登录后再改口令。",
            )
        # ★ 先写私钥密文（不提交），再落哈希，**同一个事务里一起提交**。
        #   任何一步失败 ⇒ 两个都没变 ⇒ 账号仍然是"旧口令能正常登录"。
        with collect() as sw:
            mgr.rewrap_user_key(user.username, sk, body.password, db=db)
        user.pwd_hash = hash_password(body.password)
        db.commit()
        audit(db, actor.username, "user_patch", user.username, detail="口令已更换（私钥已重封）")
        out = user_public(user)
        out["timings"] = sw.payload()
        metrics.record("rewrap", sw.total_ms(), sw.rows())
        return out

    db.commit()
    db.refresh(user)
    audit(db, actor.username, "user_patch", user.username)
    return user_public(user)


@router.delete("/users/{user_id}")
def delete_user(
    user_id: int,
    admin: UserRow = Depends(require_admin),
    mgr: StoreManager = Depends(get_manager),
    db: Session = Depends(get_db),
):
    user = db.get(UserRow, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "用户不存在")
    if user.username == admin.username:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "不能删除自己")
    username = user.username
    uid = user.id
    # ★ 墓碑名：旧数据改挂到它名下。
    #
    #   为什么必须改名而不是直接删行：
    #   登记表（``core.registry.BlockRegistry``）与向量是从 ``blocks`` 表
    #   重建的（见 ``manager._reload``），而且下标必须**密集**：
    #   删掉任何一行都会让后面所有下标错位，下次启动直接报"登记表重建错位"。
    #   向量上的块只支持从**末尾**截断，压在中间的那些删不掉。
    #
    #   改名则完全不动下标、不动任何代数量 —— 这些块照样能被任何人验证。
    #   改了名之后，同名重建的新账号与它们不再同名，界面上那个"可解密"
    #   判据就**如实**了（不再出现"列表说能解、点了 403"）。
    #
    #   ★ 名字里带 ``#id`` 是为了唯一：``ghost`` 被删两次会有两个墓碑名，
    #     否则 (owner, file_key) 会撞唯一约束。
    #     （SQLite 不强制 VARCHAR 长度，所以名字超过 64 字符也不会被截断出错。）
    tomb = f"{username}（已删号#{uid}）"
    n_files = db.execute(
        select(func.count()).select_from(FileRow).where(FileRow.owner == username)
    ).scalar_one()

    # 先改内存（它会先做冲突检查，抛了就不会动库），再改库，最后删用户。
    # 顺序反过来会留下"库已改、内存没改"的分叉 —— 而分叉只有重启后才看得出来。
    #
    # ★ 库那半边失败就把内存改回去：分叉的后果是"重启前后文件列表不一样"，
    #   那种故障最难查。这里宁可整体失败也不留下一半。
    mgr.store.rename_owner(username, tomb)
    try:
        db.execute(
            update(BlockRow).where(BlockRow.owner == username).values(owner=tomb)
        )
        db.execute(update(FileRow).where(FileRow.owner == username).values(owner=tomb))
        db.delete(user)
        db.commit()
    except Exception:
        db.rollback()
        mgr.store.rename_owner(tomb, username)
        raise
    audit(
        db,
        admin.username,
        "user_delete",
        username,
        detail=(
            f"该用户名下的 {n_files} 份文件已改挂到墓碑名 {tomb}"
            f"（仍在向量里、仍可验证，但从此没有任何人能解密）"
        ),
    )
    return {
        "ok": True,
        "deleted": username,
        "files_left": n_files,
        "tomb_name": tomb,
        "warning": (
            f"{username} 已删除，用户名可以重新使用了。\n\n"
            f"他名下的 {n_files} 份文件还在全局向量里（方案上物理删不掉："
            f"登记表与向量是从 blocks 表重建的，只支持从末尾截断），"
            f"现在挂在墓碑名 {tomb} 下面：\n"
            f"  · 任何人仍然可以验证它们的完整性（验证不受限）；\n"
            f"  · 但从此以后没有任何人能解开它们 —— 包括管理员"
            f"（块密钥是用他当时的公钥封的，没有第二把钥匙）。\n\n"
            f"重新建一个同名的 {username} 不会继承上面任何东西："
            f"他会拿到一对全新的密钥，那些旧文件在他眼里是 🔒。"
        ),
    }


@router.get("/audit")
def list_audit(
    limit: int = 200,
    target: str | None = None,
    actor: str | None = None,
    _: UserRow = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """审计流水（**最新在前**）。

    ``target`` / ``actor`` 是做「回放」用的**精确匹配**过滤，两者可同时给（交集）。
    刻意不做模糊搜索 —— 回放要的是「这一个对象 / 这一个人」，含糊匹配只会
    把别人的记录混进来，那比不过滤更糟。

    * ``target`` 形如 ``所有者/文件标识``：**文件级**动作（上传、改块、追加、
      授权、解密、解密被拒）都用这个键，所以按它能把一份文件的完整经历捞出来；
    * ``actor`` 是操作者。**查询类动作只能靠它** —— 查询是按全局下标记的
      （``target`` 是"12 块"这种摘要），本来就不落在某个具体文件上。

    只记元数据这一条不变：返回体里永远不含明文、密钥或密文。
    """
    limit = max(1, min(limit, 1000))
    stmt = select(AuditRow)
    if target:
        stmt = stmt.where(AuditRow.target == target)
    if actor:
        stmt = stmt.where(AuditRow.actor == actor)
    rows = db.execute(stmt.order_by(AuditRow.id.desc()).limit(limit)).scalars().all()
    return [
        {
            "id": r.id,
            "ts": r.ts.isoformat(timespec="seconds"),
            "actor": r.actor,
            "action": r.action,
            "target": r.target,
            "ok": r.ok,
            "detail": r.detail,
        }
        for r in rows
    ]


@router.post("/check")
def run_check(
    admin: UserRow = Depends(require_admin),
    mgr: StoreManager = Depends(get_manager),
    db: Session = Depends(get_db),
):
    """跑一遍 ``store.check()`` —— 登记表一致、每台服务器视图合法、
    声称与实存一致、**增量摘要 == 一次性承诺**。
    """
    try:
        with collect() as sw:
            mgr.check()
    except Exception as exc:  # noqa: BLE001 - 自检失败要把原因原样给出
        audit(db, admin.username, "check", ok=False, detail=str(exc))
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"自检失败：{exc}") from exc
    audit(db, admin.username, "check", ok=True)
    metrics.record("check", sw.total_ms(), sw.rows())
    return {
        "ok": True,
        "message": "自检通过：登记表 / 节点视图 / 增量摘要与一次性承诺一致",
        "timings": sw.payload(),
    }
