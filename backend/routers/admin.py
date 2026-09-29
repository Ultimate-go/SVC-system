"""管理员路由：增删用户、看审计流水、触发全量自检、改**服务器台数**。

**只有 ``admin`` 角色能进**（``Depends(require_admin)``）。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .. import metrics
from ..config import (
    DEFAULT_BACKEND_PORT,
    DEFAULT_FRONTEND_PORT,
    DEFAULT_NODE_BASE_PORT,
    DEFAULT_NODE_COUNT,
    MAX_NODE_COUNT,
    MAX_PORT,
    MIN_NODE_COUNT,
    MIN_PORT,
    DeployPorts,
    check_port_conflicts,
    clamp_node_count,
    effective_node_ids,
    effective_ports,
    node_ports_for,
    port_in_use,
    read_deploy_info,
    read_deploy_node_count,
    read_deploy_ports,
    read_deploy_stop_ports,
    write_deploy_node_count,
    write_deploy_ports,
)
from ..deps import audit, current_token, current_user, get_db, get_manager, require_admin
from ..manager import StoreManager
from ..models import AuditRow, BlockRow, FileRow, UserRow
from ..schemas import DeployIn, PortsIn, RestartIn, UserCreateIn, UserPatchIn
from ..security import hash_password
from core.timing import collect
from .auth import user_public

router = APIRouter(prefix="/api/admin", tags=["admin"])

#: 项目根目录 —— 「立刻重启」要用**绝对路径**把 ``scripts/restart.py`` 拉起来。
#: 后端的工作目录未必是项目根（跑测试时就不是），所以不能用相对路径。
ROOT = Path(__file__).resolve().parents[2]


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

    ``ok=True`` 之外还可能带 ``notes``：那是**提示**，不是错误。
    目前只有一种 —— 缩容保存之后、重启之前，被摘掉的机器还会留着旧副本
    （它们已不参与分发与检索，而且那几份副本删不掉）。
    """
    try:
        with collect() as sw:
            notes = mgr.check()
    except Exception as exc:  # noqa: BLE001 - 自检失败要把原因原样给出
        audit(db, admin.username, "check", ok=False, detail=str(exc))
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"自检失败：{exc}") from exc
    audit(db, admin.username, "check", ok=True, detail="；".join(notes))
    metrics.record("check", sw.total_ms(), sw.rows())
    return {
        "ok": True,
        "message": "自检通过：登记表 / 节点视图 / 增量摘要与一次性承诺一致",
        "notes": notes,
        "timings": sw.payload(),
    }


# ---------------------------------------------------------------------------
# 服务器台数（部署规模）
#
# 一条链，四个入口：
#
#   GET  /api/admin/deploy          现在几台 / 配置里几台 / 要不要重启
#   GET  /api/admin/deploy/plan     改成 N 台会怎样（**只算不动**，给界面先看）
#   PUT  /api/admin/deploy          保存（台数变小 ⇒ **先搬块、再写配置**）
#   POST /api/admin/deploy/restart  一键重启（拉起重启器，按配置里的台数重来）
#
# ★★ 这个文件里最要紧的一条时序：**缩容必须在重启之前把块搬走**。
#    改小台数之后、重启之前，被摘掉的那几台**还活着** —— 它们手里的密文读得出来、
#    证据也拆得出来。一旦重启，那些进程就没了，那时再想搬，源已经下线，
#    块就是真丢了。所以 PUT 与 restart 都是「先 redistribute、再写配置」。
#
# ★★ 下面这些文本会**原样**显示在界面上，而 `ElMessageBox` / `ElMessage` /
#    `el-alert` 收的都是**纯文本**（不是 Markdown）—— 所以一个 ``**`` 都不能写：
#    写进去就会连同星号一起显示出来。要强调就用「」。
# ---------------------------------------------------------------------------


def _deploy_keep(mgr: StoreManager, want: int) -> tuple[str, ...]:
    """台数变小时，块最终落在哪几台 —— **保留前 N 台**。

    规则刻意简单：按集群当前顺序切前 N 台。为什么不挑"数据最少的那几台"之类：

    * 挑法一旦"聪明"，使用者就无法在**点保存之前**预测"还剩哪几台"，
      而这件事必须能提前看到（``/deploy/plan`` 会返回 ``will_be_removed``）；
    * 顺序本来就有意义（前端按集群顺序显示、分片轮转也按这个顺序转），
      顺着它切一刀是最容易解释、也最容易复现的。
    """
    return tuple(list(mgr.store.node_ids)[: max(0, int(want))])


def _deploy_shrink_guard(mgr: StoreManager, want: int, confirm: bool) -> dict:
    """缩容前的**那道门**。返回计划；需要确认却没确认时抛 409。

    两种情形分开说，因为它们的后果差得很远：

    * ``lost`` —— 有块**每一份副本都在联系不上的机器上**。现在收缩就是永久丢失。
      这是唯一一条真会丢数据的路，必须把下标列出来让人自己决定；
    * ``orphan`` —— 有块只是**要搬一下**。搬完不会丢任何东西，
      但它会**动到布局**（哪台存哪些块变了），所以也该先看一眼再点头。
    """
    running = len(mgr.settings.node_ids)
    plan = mgr.plan_redistribute(keep=_deploy_keep(mgr, want))
    if confirm or (not plan["orphan"] and not plan["lost"]):
        return plan

    head = f"把服务器从 {running} 台减到 {want} 台"
    if plan["lost"]:
        shown = "、".join(str(i) for i in plan["lost"][:12])
        more = f"…（共 {len(plan['lost'])} 块）" if len(plan["lost"]) > 12 else ""
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{head}，但有 {len(plan['lost'])} 块现在搬不走：\n"
            f"  下标 {shown}{more}\n"
            f"  它们的每一份副本都在联系不上的机器上"
            f"（{', '.join(plan['down']) or '——'}）。\n"
            f"  现在收缩 = 这几块永久丢失（承诺里那把锁还在，内容没了）。\n"
            f"  正确做法是先把那几台机器起起来，再收缩 —— 那时它们就搬得动了。\n"
            f"  确实要带着这个损失继续，请在界面上勾选「我知道这几块会丢」。",
        )
    raise HTTPException(
        status.HTTP_409_CONFLICT,
        f"{head}，需要重新分配 {len(plan['orphan'])} 块：\n"
        f"  这些块现在只在 {', '.join(plan['remove']) or '——'} 上还留着副本。\n"
        f"  保存时会趁那几台还活着的时候，把内容和证据搬到 "
        f"{', '.join(plan['keep'])} 上 ——\n"
        f"  块内容、全局下标和承诺一个字都不变，不会丢。\n"
        f"  确认请再点一次（界面上会问你一句）。",
    )


def _deploy_note(running: int, want: int, moved: dict | None) -> str | None:
    """给界面看的一整句话。``None`` = 台数没变，没什么可说的。"""
    if moved is not None:
        parts = [
            f"服务器数量减少（{running} → {want} 台），文件块已重新分配："
            f"{moved['moved_blocks']} 块已铺到剩下的 {want} 台上。"
        ]
        if moved["lost"]:
            shown = "、".join(str(i) for i in moved["lost"][:12])
            parts.append(
                f"另有 {len(moved['lost'])} 块没能搬走（每一份副本都联系不上），"
                f"下标 {shown} —— 这几块的内容已经找不回来了。"
            )
        return " ".join(parts)
    if want > running:
        return (
            f"服务器台数 {running} → {want} 台。"
            f"新加的机器一开始是空的 —— 旧块留在原处不动，"
            f"之后新写入的块才会摊到它们上。"
        )
    if want < running:
        return (
            f"服务器台数 {running} → {want} 台"
            f"（这次不需要搬块：每块在留下的机器上都还有副本）。"
        )
    return None


def _spawn_restart(script_args: list[str]) -> None:
    """把重启器作为一个**分离进程**拉起来。

    ★ 为什么是分离的、而且不能再由后端自己接着做：后端也是要被停掉的那几样之一，
      ``taskkill /F`` 打完自己那一发之后就没机会再把它拉起来了（详见
      ``scripts/restart.py`` 的文件头）。

    ★ ``CREATE_NEW_CONSOLE``：让它有**自己的窗口**，一是能看见它在干什么，
      二是后端被停掉时不会把它的控制台一起带走。
    """
    script = ROOT / "scripts" / "restart.py"
    if not script.exists():
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            f"找不到重启脚本：{script}（工程被挪过？）",
        )
    cmd = [sys.executable, str(script), *script_args]
    flags = subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0
    try:
        subprocess.Popen(cmd, cwd=str(ROOT), creationflags=flags)
    except OSError as exc:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, f"拉不起重启脚本：{exc}"
        ) from exc


@router.get("/deploy")
def get_deploy(
    admin: UserRow = Depends(require_admin),
    mgr: StoreManager = Depends(get_manager),
):
    """现在的部署状态：**跑着几台**（进程里的真值）/ **配置里几台**（重启后生效）。

    两个数是分开的，因为"改台数 = 重启后生效"这条模型的核心就是它们会短暂不一致
    —— 界面必须能同时看到这两件事，否则用户点了保存却看不出"到底生效没有"。
    """
    running_ids = list(mgr.settings.node_ids)
    running = len(running_ids)
    saved = read_deploy_node_count()
    info = read_deploy_info()
    restart_required = saved != running

    prev_raw = info.get("prev_count")
    change = None
    if prev_raw is not None:
        try:
            prev = int(prev_raw)
        except (TypeError, ValueError):
            prev = running
        # 只在"真的变了"时给：重启完之后 prev 与 running 一致，那句话就不该再出现。
        if prev != running:
            change = {"prev": prev, "now": running, "to_be": saved}

    note = str(info.get("note") or "")
    if note:
        notice = note + (f" 点「立刻重启」让 {saved} 台生效。" if restart_required else "")
    elif restart_required:
        notice = (
            f"服务器台数已从 {running} 台改成 {saved} 台，重启后生效 —— "
            f"现在跑的还是 {running} 台（{', '.join(running_ids)}）。"
        )
    else:
        notice = ""

    return {
        "node_count": saved,
        "running_count": running,
        "running_node_ids": running_ids,
        "restart_required": restart_required,
        "count_change": change,
        #: 本次启动才加入的机器（还没有块）—— 与"老节点恰好一块没分到"不是一回事。
        "fresh_nodes": mgr.joined_now(),
        "last_redistribute": mgr.last_redistribute(),
        "notice": notice,
        "min": MIN_NODE_COUNT,
        "max": MAX_NODE_COUNT,
        "default": DEFAULT_NODE_COUNT,
        "replica_factor": mgr.store.replica_factor,
        "blocks": mgr.store.n,
        #: 台数存在哪 —— 界面上要能把它指出来（"改的是哪个文件"）。
        "source": "nodes/deploy.json",
    }


@router.post("/deploy/redistribute")
def redistribute_now(
    admin: UserRow = Depends(require_admin),
    mgr: StoreManager = Depends(get_manager),
    db: Session = Depends(get_db),
):
    """**手工**把不齐的副本补齐（不涉及改台数）。

    什么时候用得上：某台机器被摘掉过、后来又加回来，于是有些块的副本数
    少于 ``replica_factor``（``store.under_replicated()`` 会报出来）。
    正常情况下台数一变就会自动搬，这个入口是给"状态已经歪了、想手动抹平"用的。

    ★ 它**不改台数**：``keep`` 用当前全部机器，所以不会摘掉任何一台。
    """
    with collect() as sw:
        out = mgr.redistribute(reason="手工触发：把不足的副本补齐")
    audit(
        db,
        admin.username,
        "deploy_redistribute",
        target=f"{out['moved_blocks']} 块",
        detail=f"补了 {out['moved_copies']} 份副本" + (
            f"，{len(out['lost'])} 块搬不动" if out["lost"] else ""
        ),
    )
    return {
        "ok": True,
        "message": (
            f"补了 {out['moved_blocks']} 块（{out['moved_copies']} 份副本）"
            + (f"；另有 {len(out['lost'])} 块搬不动：" f"{out['lost'][:12]}" if out["lost"] else "")
        ),
        **out,
        "timings": sw.payload(),
    }


@router.get("/deploy/plan")
def deploy_plan(
    node_count: int,
    admin: UserRow = Depends(require_admin),
    mgr: StoreManager = Depends(get_manager),
):
    """**只算不动**：改成 ``node_count`` 台会怎样。

    界面在保存之前先问它一次，把「要摘掉哪几台 / 要重分配多少块 / 有没有搬不动的」
    摆在按钮旁边 —— 那些话必须出现在**点击之前**，点完再说就晚了。
    """
    running = len(mgr.settings.node_ids)
    want = clamp_node_count(node_count)
    shrinking = want < running
    keep = _deploy_keep(mgr, want) if shrinking else tuple(mgr.store.node_ids)
    plan = mgr.plan_redistribute(keep=keep)
    return {
        "node_count": want,
        "requested": node_count,
        "clamped": want != node_count,
        "running_count": running,
        "keep": list(keep),
        "will_be_removed": list(mgr.store.node_ids)[want:] if shrinking else [],
        # 名字按约定推：新机器是 node-<n>。起点用**当前台数 + 1**，
        # 而不是 keep 里最大那个编号 —— 摘掉的是末尾几台，两者恰好一致。
        "will_be_added": (
            [f"node-{i}" for i in range(running + 1, want + 1)] if want > running else []
        ),
        "orphan": plan["orphan"],
        "lost": plan["lost"],
        "blocks": plan["blocks"],
        "safe": plan["safe"],
        "alive": plan["alive"],
        "down": plan["down"],
        "replica_factor": plan["replica_factor"],
        "wanted": plan["wanted"],
    }


@router.put("/deploy")
def set_deploy(
    body: DeployIn,
    admin: UserRow = Depends(require_admin),
    mgr: StoreManager = Depends(get_manager),
    db: Session = Depends(get_db),
):
    """保存台数（**重启后生效**）。台数变小的话，**先搬块、再写配置**。

    顺序不能反：配置一旦写下，重启之后就只剩新台数了，被摘掉的机器全下线，
    那时再想搬块，源已经没了。所以这里先把块搬到留下的机器上（趁它们还活着），
    成功了才落配置。
    """
    running = len(mgr.settings.node_ids)
    want = clamp_node_count(body.node_count)
    clamped = want != body.node_count

    moved: dict | None = None
    timings: dict = {}
    if want < running:
        _deploy_shrink_guard(mgr, want, body.confirm_shrink)
        with collect() as sw:
            moved = mgr.redistribute(
                keep=_deploy_keep(mgr, want),
                reason=f"服务器从 {running} 台减到 {want} 台，先重新分配文件块",
            )
        timings = sw.payload()

    note = _deploy_note(running, want, moved)
    write_deploy_node_count(want, prev=running if want != running else None, note=note)
    audit(
        db,
        admin.username,
        "deploy_change",
        target=f"{running}->{want}",
        detail=note or "台数未变",
    )

    if moved is not None:
        msg = (
            f"服务器数量减少，文件块已重新分配：{moved['moved_blocks']} 块"
            f"已铺到剩下的 {want} 台上。"
        )
        if moved["lost"]:
            msg += f"\n另有 {len(moved['lost'])} 块没能搬走 —— 每一份副本都联系不上。"
        msg += f"\n点「立刻重启」让这 {want} 台生效。"
    elif want == running:
        msg = f"台数没变（还是 {running} 台）。"
    else:
        msg = (
            f"台数已从 {running} 台改成 {want} 台，重启后生效"
            f"（现在跑的还是 {running} 台）。\n点「立刻重启」即可。"
        )
    if clamped:
        msg += f"\n（{body.node_count} 超出台数范围，取 {want}）"

    return {
        "ok": True,
        "message": msg,
        "node_count": want,
        "running_count": running,
        "restart_required": want != running,
        "clamped": clamped,
        "redistribute": moved,
        "notice": note,
        "timings": timings,
    }


@router.post("/deploy/restart")
def restart_deploy(
    body: RestartIn | None = None,
    admin: UserRow = Depends(require_admin),
    mgr: StoreManager = Depends(get_manager),
    db: Session = Depends(get_db),
):
    """**一键重启**：停掉现有的节点/后端/前端，再按配置里的台数起一遍。

    它自己**不停**任何东西 —— 只把 ``scripts/restart.py`` 拉起来就返回。
    理由见那个脚本的文件头：后端也是要被停掉的东西之一，
    停自己是它做不了的事。

    请求体可以整个不给（走"按配置重启"）。给了 ``node_count`` 就等于
    "顺带把台数也改了" —— 那时会先补一次缩容搬迁（如果还没搬过）。
    """
    running = len(mgr.settings.node_ids)
    saved = read_deploy_node_count()
    explicit = body is not None and body.node_count is not None
    want = clamp_node_count(body.node_count) if explicit else saved

    moved: dict | None = None
    if want < running:
        # ★ 保存那一步通常已经搬过了（那时机器还活着），这时 plan 会是空的，
        #   于是下面什么都不做 —— **幂等**。但"先保存、过一会儿才点重启"
        #   这条路上状态可能已经变了（某台掉线了），所以这里再算一次。
        plan = mgr.plan_redistribute(keep=_deploy_keep(mgr, want))
        if plan["orphan"] or plan["lost"]:
            if not (body.confirm_shrink if body else False):
                _deploy_shrink_guard(mgr, want, confirm=False)
            moved = mgr.redistribute(
                keep=_deploy_keep(mgr, want),
                reason=f"重启前补一次缩容搬迁（{running} → {want} 台）",
            )

    if explicit or want != saved:
        note = _deploy_note(running, want, moved)
        write_deploy_node_count(
            want, prev=running if want != running else None, note=note
        )
    else:
        note = None

    _spawn_restart(["--nodes", str(want)])
    audit(
        db,
        admin.username,
        "deploy_restart",
        target=f"{running}->{want}",
        detail=note or "按当前配置重启",
    )

    msg = f"正在重启：{running} 台 → {want} 台。"
    if moved is not None:
        msg += f"\n重启前又把 {moved['moved_blocks']} 块文件块重新分配了一遍。"
    msg += (
        "\n一个独立窗口正在停机并重新启动 —— 请等它打印出"
        "「前端就绪」再刷新页面（这个页面马上会连不上，属正常）。"
    )
    # 端口改过的话，重启后**地址就换了**：这句话必须在这里说 ——
    # 不然使用者只知道"等它就绪"，却不知道去哪儿等（旧地址已经没人听了）。
    eff, _ = effective_ports()
    now_frontend = _running_ports(mgr)["frontend"]
    if now_frontend is not None and now_frontend != eff.frontend:
        msg += (
            f"\n注意：前端端口也换了 —— 重启后在 http://127.0.0.1:{eff.frontend} 打开"
            f"（这个地址会失效，重启器会自动帮你开一个）。"
        )
    return {
        "ok": True,
        "restarting": True,
        "message": msg,
        "from": running,
        "to": want,
        "restart_required": want != running,
        "redistribute": moved,
    }


# ---------------------------------------------------------------------------
# 端口（后端 / 前端 / 每一台存储节点）
#
# 与上面那族（台数）是**同一套模型**：
#
#   GET  /api/admin/ports        现在跑着哪几个 / 配置里是哪几个 / 要不要重启
#   POST /api/admin/ports/plan   **只探不写**：每个端口空不空、有没有填重
#   PUT  /api/admin/ports        保存（重启后生效）
#
# ★★ 一条硬分寸：**"自己撞自己"必须拦，"被别的程序占用"只提示。**
#    前者（两样东西填同一个端口）是确定起不来 —— 重启时后起的那个一定 bind 失败；
#    后者是使用者机器上的状态，他可能就是想先把配置存下来、过后再腾端口。
#
# ★★ 另一条：端口改了之后，「立刻重启」必须能停掉**旧端口上**那一套。
#    所以 PUT 在写新端口**之前**先把"现在真正在跑的"并进 ``stop_ports``
#    （理由见 backend/config.py 里 stop_ports 那段注释）。
#
# ★★ 下面这些文本会**原样**显示在界面上（ElMessageBox / ElMessage / el-alert
#    收的都是纯文本），所以一个 ``**`` 都不能写 —— 要强调就用「」。
# ---------------------------------------------------------------------------


def _env_port(name: str) -> int | None:
    """启动器注入的"我现在跑在哪个端口上"（见 ``scripts/start_all.node_env``）。

    ★ 为什么需要它：后端**不知道自己绑在哪个端口**（``--port`` 是 uvicorn 的
      命令行参数，应用里看不到），而界面必须回答得了「现在跑的是 X / 配置是 Y」。
      所以启动器在起后端时把这两个值写进环境变量。
      手动起（``uvicorn --port 8099``）时它不存在 → 返回 ``None``，
      界面就如实显示"当前端口未知" —— **不猜**。
    """
    raw = os.environ.get(name, "").strip()
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return None
    return n if MIN_PORT <= n <= MAX_PORT else None


def _port_of_url(url: object) -> int | None:
    """从 ``http://127.0.0.1:9101`` 里取出端口号。取不到 = ``None``。"""
    try:
        return urlsplit(str(url)).port
    except ValueError:
        return None


def _running_ports(mgr: StoreManager) -> dict:
    """**现在真正在跑的**那一套端口。拿不到的项就是 ``None``（不猜）。"""
    nodes: dict[str, int] = {}
    for nid, url in (mgr.settings.node_urls or {}).items():
        got = _port_of_url(url)
        if got:
            nodes[str(nid)] = got
    return {
        "backend": _env_port("VDS_BACKEND_PORT"),
        "frontend": _env_port("VDS_FRONTEND_PORT"),
        "nodes": nodes,
        #: 单进程模式（节点是同一个进程里的对象）—— 那时**没有**节点端口可谈。
        "distributed": bool(mgr.settings.node_urls),
    }


def _ports_restart_required(effective: DeployPorts, running: dict) -> bool:
    """配置与正在跑的是不是不一致。

    ★ 只在**两边都说得出**的时候才比：当前端口未知（不是一键启动起的）时
      返回 ``False`` —— 不能凭猜说"要重启"。
    ★ 只比两边都有的那几台节点：台数刚改过时多出来/少掉的那几台，是
      「服务器台数」那张卡片在讲的事，不在这里重复报。
    """
    for key in ("backend", "frontend"):
        now = running.get(key)
        if now is not None and now != getattr(effective, key):
            return True
    for nid, port in effective.nodes.items():
        now = running["nodes"].get(nid)
        if now is not None and now != port:
            return True
    return False


def _ports_diff(old: DeployPorts, new: DeployPorts) -> list[str]:
    """「后端 8000 → 9000」这样的短句（给界面与审计用）。"""
    out: list[str] = []
    if old.backend != new.backend:
        out.append(f"后端 {old.backend} → {new.backend}")
    if old.frontend != new.frontend:
        out.append(f"前端 {old.frontend} → {new.frontend}")
    for nid, port in new.nodes.items():
        was = old.nodes.get(nid)
        if was is not None and was != port:
            out.append(f"{nid} {was} → {port}")
    return out


def _probe_ports(ports: DeployPorts, running: dict) -> list[dict]:
    """逐个探活。每个端口一个状态：

    * ``free``     现在空着，可以用；
    * ``ours``     本系统**当前**就在用（改了它、还没重启，属正常）；
    * ``occupied`` 有别的程序在占 —— 重启时这一样会起不来。

    ★ 判据是**真的去 bind 一下**（见 :func:`~backend.config.port_in_use`）。
      同一端口只探一次（后端与前端撞着时也一样），省得白探。
    """
    ours = {
        p
        for p in (
            running.get("backend"),
            running.get("frontend"),
            *running["nodes"].values(),
        )
        if p
    }
    rows: list[tuple[str, str, int]] = [
        ("backend", "后端", ports.backend),
        ("frontend", "前端", ports.frontend),
        *[("node", nid, port) for nid, port in ports.nodes.items()],
    ]
    seen: dict[int, str] = {}
    out: list[dict] = []
    for scope, label, port in rows:
        if port not in seen:
            if not port_in_use(port):
                seen[port] = "free"
            elif port in ours:
                seen[port] = "ours"
            else:
                seen[port] = "occupied"
        status = seen[port]
        out.append(
            {
                "scope": scope,
                "id": label,
                "port": port,
                "status": status,
                "note": {
                    "free": "空闲",
                    "ours": "本系统当前就在用（重启时会让出来）",
                    "occupied": "有别的程序在占用，重启时这一样会起不来",
                }[status],
            }
        )
    return out


def _port_warnings(probe: list[dict]) -> list[str]:
    """把占用情况收成**给界面看的一整句话**（空列表 = 没有要提醒的）。"""
    bad = [x for x in probe if x["status"] == "occupied"]
    if not bad:
        return []
    shown = "、".join(f"{x['id']} {x['port']}" for x in bad[:10])
    more = f"（共 {len(bad)} 个）" if len(bad) > 10 else ""
    return [
        f"{shown}{more} 现在有别的程序在占用 —— 重启时这几样会起不来。"
        f"换一个端口，或者先把占用它的那个程序停掉。"
    ]


def _ports_notice(effective: DeployPorts, running: dict) -> str:
    """「改了端口还没重启」那一条提示（仅在真的要重启时给）。"""
    diffs: list[str] = []
    for key, label in (("backend", "后端"), ("frontend", "前端")):
        now = running.get(key)
        if now is not None and now != getattr(effective, key):
            diffs.append(f"{label} {now} → {getattr(effective, key)}")
    for nid, port in effective.nodes.items():
        now = running["nodes"].get(nid)
        if now is not None and now != port:
            diffs.append(f"{nid} {now} → {port}")
    body = f"：{'；'.join(diffs)}。" if diffs else "。"
    # ★ 只在前端端口**真的变了**时才提"新地址"：没变的时候说"这个页面会失效"
    #   是错的（地址没变，刷新一下就行）—— 提示里说错一句，使用者就会白找一圈。
    now_frontend = running.get("frontend")
    if now_frontend is not None and now_frontend != effective.frontend:
        tail = (
            f"重启后前端在 http://127.0.0.1:{effective.frontend} —— "
            f"现在这个页面会失效，重启器会自动在新地址开一个。"
        )
    else:
        tail = "重启期间页面会连不上，起来之后刷新一下即可（要重新登录一次）。"
    return f"端口改了，还没重启{body}{tail}"


def _ports_payload(mgr: StoreManager, *, warnings: list[str] | None = None) -> dict:
    """``GET /api/admin/ports`` 的响应体；``PUT`` 复用它，省得两份字段名分叉。"""
    ids = effective_node_ids()
    effective, notes = effective_ports(ids)
    running = _running_ports(mgr)
    restart_required = _ports_restart_required(effective, running)
    return {
        "backend": effective.backend,
        "frontend": effective.frontend,
        "nodes": dict(effective.nodes),
        "node_ids": list(effective.nodes),
        "running": running,
        "restart_required": restart_required,
        "min": MIN_PORT,
        "max": MAX_PORT,
        "defaults": {
            "backend": DEFAULT_BACKEND_PORT,
            "frontend": DEFAULT_FRONTEND_PORT,
            "node_base": DEFAULT_NODE_BASE_PORT,
        },
        #: 台数与端口存在同一个文件里 —— 界面把这句话指出来（"改的是哪个文件"）。
        "source": "nodes/deploy.json",
        "notice": _ports_notice(effective, running) if restart_required else "",
        "notes": notes,
        "warnings": list(warnings or []),
    }


def _candidate_ports(body: PortsIn) -> tuple[DeployPorts, dict[str, int], list[str]]:
    """把请求体收成一套完整端口。

    返回 ``(完整的一套, 使用者实际填的那几个, 自己撞自己的清单)``。

    ★ 冲突检查必须看**使用者填的原始值**，不能看补全之后的结果：补全那一步
      （:func:`~backend.config.node_ports_for`）会把重复的端口让开，于是
      "两个节点填了同一个端口"会被**悄无声息地抹平**，而使用者以为自己填的生效了。
      所以两份都算出来。
    """
    ids = effective_node_ids()
    for label, port in (("后端端口", body.backend), ("前端端口", body.frontend)):
        if not (MIN_PORT <= int(port) <= MAX_PORT):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"{label}超出范围：{port}。允许 {MIN_PORT}–{MAX_PORT}。",
            )
    raw: dict[str, int] = {}
    if body.nodes is not None:
        for nid, port in body.nodes.items():
            name = str(nid).strip()
            if name not in ids:
                # 界面不会这么发（它按配置里的台数列行）；真有就当没看见。
                continue
            if not (MIN_PORT <= int(port) <= MAX_PORT):
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    f"{name} 的端口超出范围：{port}。允许 {MIN_PORT}–{MAX_PORT}。",
                )
            raw[name] = int(port)
    conflicts = check_port_conflicts(body.backend, body.frontend, raw)
    nodes = node_ports_for(ids, raw, reserved=(body.backend, body.frontend))
    return (
        DeployPorts(int(body.backend), int(body.frontend), nodes),
        raw,
        conflicts,
    )


def _conflict_detail(conflicts: list[str]) -> str:
    """端口填重了那 409 的正文（要短、要能照着改）。"""
    return (
        "同一个端口只能给一样东西用 —— 下面这几处撞了，重启时后起的那个一定起不来：\n  "
        + "\n  ".join(conflicts)
        + "\n\n改掉其中一方再保存。"
    )


def _stop_ports_to_keep(running: dict) -> list[int]:
    """本次改动之前，**可能还有进程占着**的那些端口（并进 ``stop_ports``）。

    ★ 三处来源都要，而且**取并集**：

    * ``stop_ports`` —— 历史上用过、已经记下的（连改好几次时全靠它）；
    * ``ports``      —— 上一次保存下来的那一套（可能还没重启，也可能已生效）；
    * ``running``    —— 现在真正在跑的（最权威的一份）。

      少了任何一处，停止流程就可能漏掉一批端口，留下占着端口的孤儿进程。
    ★ 并集是**安全**的：停止时多扫一个端口只是多一次探测；而真正会不会被杀，
      还要过一遍"这是不是本系统的进程"（见 ``scripts/start_all.ours_like``），
      所以不会误杀别人的程序。
    """
    ports = read_deploy_stop_ports() | set(read_deploy_ports().all_ports())
    ports |= {int(p) for p in running["nodes"].values()}
    for key in ("backend", "frontend"):
        got = running.get(key)
        if got:
            ports.add(int(got))
    return sorted(ports)


@router.get("/ports")
def get_ports(
    admin: UserRow = Depends(require_admin),
    mgr: StoreManager = Depends(get_manager),
):
    """端口现状：**下一轮要用的那一套** + **现在跑着的那一套**。

    与台数那张卡片同一个模型：两边分开给，界面才回答得了「改了到底生效没有」。
    """
    return _ports_payload(mgr)


@router.post("/ports/plan")
def plan_ports(
    body: PortsIn,
    admin: UserRow = Depends(require_admin),
    mgr: StoreManager = Depends(get_manager),
):
    """**只探不写**：每个端口空不空、有没有填重。

    界面在保存**之前**调它 —— 那些话必须出现在点击之前，点完再说就晚了。
    """
    candidate, _, conflicts = _candidate_ports(body)
    probe = _probe_ports(candidate, _running_ports(mgr))
    return {
        "ok": not conflicts,
        "conflicts": conflicts,
        "probe": probe,
        "warnings": _port_warnings(probe),
        "detail": _conflict_detail(conflicts) if conflicts else "",
    }


@router.put("/ports")
def set_ports(
    body: PortsIn,
    admin: UserRow = Depends(require_admin),
    mgr: StoreManager = Depends(get_manager),
    db: Session = Depends(get_db),
):
    """保存端口（**重启后生效**）。

    两件事分开处理（这是这个需求里最要紧的一条分寸）：

    * **填重了 → 409**：那是确定起不来，不能让使用者在"看起来保存成功"之后
      才发现有一台起不来；
    * **被别人占了 → 存下来 + 一条警告**：那是他机器上的状态，不是配置的错。
      他可能就是要先把配置存下、过后再腾端口。
    """
    candidate, _, conflicts = _candidate_ports(body)
    if conflicts:
        raise HTTPException(status.HTTP_409_CONFLICT, _conflict_detail(conflicts))

    ids = effective_node_ids()
    old, _ = effective_ports(ids)
    running = _running_ports(mgr)
    # ★ 先记旧的，再写新的（顺序反过来就可能把"正在跑的那一套"弄丢）。
    keep = _stop_ports_to_keep(running)
    write_deploy_ports(
        candidate.backend, candidate.frontend, candidate.nodes, also_stop=keep
    )

    probe = _probe_ports(candidate, running)
    warnings = _port_warnings(probe)
    diffs = _ports_diff(old, candidate)
    audit(
        db,
        admin.username,
        "deploy_ports",
        target="；".join(diffs) or "端口未变",
        detail=(
            f"后端 {candidate.backend} / 前端 {candidate.frontend} / "
            + ", ".join(f"{k}={v}" for k, v in candidate.nodes.items())
        ),
    )

    if diffs:
        msg = "端口已保存：" + "；".join(diffs) + "。重启后生效。"
    else:
        msg = "端口没变，配置已按当前值写回。"
    if any("前端" in d for d in diffs):
        msg += (
            f"\n重启后前端在 http://127.0.0.1:{candidate.frontend}"
            f" —— 这个地址会失效，重启器会自动在新地址开一个页面。"
        )
    return {
        **_ports_payload(mgr, warnings=warnings),
        "ok": True,
        "message": msg,
        "probe": probe,
        "conflicts": [],
    }
