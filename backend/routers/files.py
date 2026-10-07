"""文件路由：上传、列表、详情、**解密**（受限）。

两条线的落点就在这个文件里：

* ``GET /api/files`` —— **验证不受限**：列出所有人的文件，谁都能选块去验证
* ``POST /api/files/{id}/decrypt`` —— **解密受限**：只有所有者能解

.. important::

   **密钥只管解密（规划 §8.6 的第③道门）**。本文件里 ``/api/query`` 的那条
   验证路线**一个字节都不碰任何密钥** —— "验证不受限"是项目的硬决策。

.. important::

   **"能不能解密"的判据只有一条：你是不是所有者。**

   应用层那道检查（:func:`can_decrypt`）只是一个**快速门**：能提前拒、
   能写审计。它**不是**第二个权限来源 —— 真正拦住人的是密码学那道门
   （块密钥是用**所有者的公钥**封的，别人的私钥解不开，见
   :meth:`~backend.manager.StoreManager.read_plain`）。

   保留两层是有意的：应用层给得出"为什么拒"的好话术，密码学层才是
   **算不算得出来**。两套规则各管一件事，不会分叉。
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import metrics
from ..deps import audit, current_token, current_user, get_db, get_manager
from ..manager import (
    Conflict,
    DecryptDenied,
    NotFound,
    OutOfRange,
    StoreManager,
)
from ..models import BlockRow, FileRow, UserRow
from ..schemas import DecryptIn, FilePatchIn, ReplayIn
from core.keywrap import KeyWrapError
from core.timing import collect
from core.transport import TransportError, WriteError

router = APIRouter(prefix="/api/files", tags=["files"])

MAX_UPLOAD_BYTES = 8 * 1024 * 1024

#: 应用层快速拒绝的话术。
#: 它与密码学那句（见 :func:`decrypt`）**意思相同但措辞不同**：
#: 这句是"还没解密就知道不是你的"，那句是"真的没解开"。
#: 分开写是为了诚实 —— 免得看日志的人以为每次都跑了一遍解密。
DENIED_NOT_OWNER = (
    "这不是你的文件，所以拿不到块密钥。"
    "注意：你仍然可以验证它的完整性 —— 验证是公开的，解密才需要私钥。"
)


def _write_failed(exc: WriteError) -> HTTPException:
    """写推失败 → **可操作**的 503（而不是当 500 内部错误搪塞过去）。

    为什么值得单独一条：这既不是"请求错了"，也不是"服务器坏了"，
    而是**全网暂时不一致** —— 而且此刻"重新上传一次"是**错**的修法
    （协调者的 δ 没推进，但登记表已经分配过那批下标了，重传会拿到新下标，
    并撞上"登记表游标与向量长度不一致"）。所以提示必须说清
    "把机器弄活 → 补推"，否则拿到 503 的人第一反应就是重传。
    """
    who = "、".join(exc.nodes) or "未知节点"
    return HTTPException(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        f"有 {len(exc.nodes)} 台存储节点没跟上这次更新（{who}）—— "
        f"全网摘要暂时不一致，已经跟上的那几台不会回退。"
        f"协调者没有推进自己的账，但登记表已经分配过这批下标，"
        f"所以不要重新上传（那会拿到新下标）。"
        f"把没通的机器弄活之后，在「集群」页点一次「补推」即可收敛"
        f"（POST /api/nodes/retry-push）；已经跟上的节点不会被重复打扰。",
    )


def _reject_if_pending(mgr: StoreManager) -> None:
    """上一次写没推全 → **先别写**。

    写推失败之后协调者的 δ 没有推进，但登记表**已经分配过**那批下标了。
    这时候再写会撞上“登记表游标与向量长度不一致” —— 那是一句 RuntimeError，
    到了客户端只是一个 500“服务器内部错误”，完全指不到该怎么办。
    所以在这里把它变成一个能看懂的 409，并直说修法（补推）。
    """
    st = mgr.pending_write()
    if not (st["pending"] or st["persist_pending"]):
        return
    pd = st["pending"] or {}
    who = "、".join(pd.get("nodes", [])) or "（未记录）"
    raise HTTPException(
        status.HTTP_409_CONFLICT,
        f"上一次写（{pd.get('op', '写推')}）还有 {len(pd.get('nodes', []))} 台存储节点没跟上（{who}），"
        f"全网摘要尚未一致 —— 请先在「集群」页点一次「补推」"
        f"（POST /api/nodes/retry-push），再继续写。"
        f"不要重新上传：登记表已经分配过那批下标，重传会拿到新下标。",
    )


def _file_or_404(db: Session, file_id: int) -> FileRow:
    row = db.get(FileRow, file_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"文件 {file_id} 不存在")
    return row


def can_decrypt(user: UserRow, row: FileRow) -> bool:
    """这个人能不能解密这份文件 —— **只看是不是所有者**。

    列表页那颗锁图标就是拿它渲染的。它**不查表、不算策略、不做标量乘**，
    纯一道身份判断，所以列表页可以直接用。

    ★ 为什么不再有"管理员特例"：以前管理员靠的是 ABE 的**主密钥**
      （KGC 特权，能签发任意属性的密钥）。现在没有这种东西了 ——
      块密钥是拿**所有者的公钥**封的，**没有人**手里有能解开它的第二把钥匙，
      包括管理员、包括协调者自己。

      所以这一句判定在小路径上放宽（返回 ``True``）**也不会真的放行**：
      真正拦住人的是 :meth:`StoreManager.read_plain` 里那一次解封失败。
      两层各管一件事：这层负责"好话术 + 审计"，那层负责"算不算得出来"。
    """
    return user.username == row.owner


def _file_public(row: FileRow, mgr: StoreManager) -> dict:
    store = mgr._require()
    indices = store.file_indices(row.owner, row.file_key)
    # ★ 这份文件自己的摘要。注意：本函数与 manager.list_files() 是**两份**
    #   序列化实现，字段容易分叉 —— 列表页要的 segments 就只长在那一份上，
    #   而路由走的是本函数，于是界面上「位置段」永远显示 "—"。
    _fd_row = store.delta_of(row.owner, row.file_key)
    return {
        "id": row.id,
        "owner": row.owner,
        "file_key": row.file_key,
        "total_bytes": row.total_bytes,
        "block_count": row.block_count,
        "version": row.version,
        "segment_bytes": row.segment_bytes,
        # ★ 这是**上传时刻**整份明文的摘要，改块之后它就不再是当前内容的指纹了。
        #   不更新它**不是**偷懒：协调者手里只有各块的密文摘要、没有整份明文，
        #   算不出新的整体摘要。所以界面必须把它标成"上传时的"。
        #   "这份文件现在第几版"的权威答案是上面的 ``version``。
        "content_digest": row.content_digest,
        "created_at": row.created_at.isoformat(timespec="seconds"),
        "indices": list(indices),
        # 下面两个只是**方便显示**的边界值。
        #
        # .. warning::
        #
        #    追加之后，一个文件的全局下标**不再保证是一段连续区间**
        #    （两次追加之间别的文件可能也占了位置）。所以
        #    ``first_index``..``last_index`` 之间可能**夹着别人的块**，
        #    不能当区间用。要“这份文件到底占哪些位置”，只能用 ``indices``。
        "first_index": indices[0] if indices else None,
        "last_index": indices[-1] if indices else None,
        # ★ 当前全局块数 n。带上它，前端才能回答"我手里那些旧证据还行吗"：
        #   证据 π_I = (S_I, Λ_I) 里 S_I = g^{e_[n]/e_I}，是 **n 的函数** ——
        #   所以任何一次上传都会让此前取到的证据全部失效（设计 B 的固有代价）。
        #   不标出来的话，界面上那些"验证通过"会一直是旧的结论。
        "delta_n": store.n,
        # ★ 但只看 n 不够：**改块不改 n、却改承诺 C**（见 manager.modify_block）。
        #   所以再给一个 δ 指纹；前端判"手里那张卡作废没有"要按它判。
        "delta_fp": mgr.delta_fingerprint(),
        # ★ 这份文件在全局位置轴上占的段（追加会让它变成好几截）。
        #   文件列表的「位置段」列渲染的就是它；没有这几个字段，那一列只能是 "—"。
        "offset": int(_fd_row.offset),
        "n": int(_fd_row.n),
        "segments": [[int(a), int(b)] for a, b in _fd_row.segments],
        #: 逐文件的 δ 指纹（与上面那个全局的分开）：判"这份文件的旧证据还行吗"用它。
        "file_delta_fp": mgr.delta_fingerprint((row.owner, row.file_key)),

    }


# ---------------------------------------------------------------------------
# 上传
# ---------------------------------------------------------------------------

@router.post("", status_code=status.HTTP_201_CREATED)
def upload(
    file: UploadFile = File(...),
    file_key: str = Form(...),
    segment_bytes: int | None = Form(default=None),
    nodes: str | None = Form(default=None),
    user: UserRow = Depends(current_user),
    mgr: StoreManager = Depends(get_manager),
    db: Session = Depends(get_db),
):
    """上传并对外分发，**同时用你的公钥封装每块的密钥**。

    谁能解密这件事在**这一刻**就定死了：块密钥是拿**上传者（所有者）的公钥**
    封的，别人的私钥对它无效。所以再没有"策略"参数 —— 也就不再有
    "策略写错了连自己都解不开"那种失败模式。

    :param segment_bytes: 这份文件按多少字节切一块。不传 = 部署默认值。
        它**逐文件记下来**（``files.segment_bytes``），以后改部署默认值不会
        把老文件弄坏；改块与追加也都按这份文件自己的值走。

        两边都有限制：下面的下限防“1 字节一块把位置上限撞了”（那时的报错
        看起来像“文件太大”，指不到真正原因），上限防“一块 1 GB 把内存吃光”。

    **用同步路由 + ``file.file.read(n)``**（而不是 ``await file.read()``）：
    向量追加要拿全局锁、还是纯 CPU 的模幂，写成异步只会阻塞事件循环；
    交给 FastAPI 的线程池更合适。

    ★ 读的时候**带上上限**（安全审计 I4）：以前是 ``file.file.read()`` ——
    先把整份读进内存，然后才判 8 MB 上限。那么 8 MB 上限根本拦不住内存峰值，
    一个 2 GB 的请求会先把进程吃掉。现在只读“上限 + 1”字节：
    多读的那 1 字节是为了区分“刚好等于上限”与“超过上限”。
    """
    data = file.file.read(MAX_UPLOAD_BYTES + 1)
    if not data:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "文件为空")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"文件超过 {MAX_UPLOAD_BYTES // 1024 // 1024} MB 上限",
        )

    # 「手动指定分发」：逗号分隔的机器名。不传 / 传空 = 全部机器（老行为）。
    picked: list[str] | None = None
    if nodes is not None and nodes.strip():
        picked = [n.strip() for n in nodes.split(",") if n.strip()]
        if not picked:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "nodes 传了但没有一个有效的机器名（应当是逗号分隔，如 node-1,node-2）",
            )

    _reject_if_pending(mgr)
    # ★ ``with collect()`` 开一段阶段秒表：下面这个调用里做的每一步
    #   （切块 / 逐块加密 / 算向量分量 / 封块密钥 / 承诺 / 分发 / 落库）
    #   都会被记下来，最后挂在响应的 ``timings`` 上给前端画进度条。
    #   没人在收集时它是**空操作** —— 见 core/timing.py。
    try:
        with collect() as sw:
            row = mgr.upload(
                user.username,
                file_key,
                data,
                segment_bytes=segment_bytes,
                nodes=picked,
            )
    except Conflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except OutOfRange as exc:
        # 块大小越界、块数超上限都走这条。
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except (KeyWrapError, NotFound) as exc:
        # 最常见的是"这个账号还没有密钥对" —— 说清楚该怎么办，
        # 不要让它变成一个 500。
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except WriteError as exc:
        # 有节点没跟上：这是**可恢复的**，而且恢复方式不是重传（见 _write_failed）
        raise _write_failed(exc) from exc

    detail = f"{len(data)} B / {row.block_count} 块 / 每块 {row.segment_bytes} B"
    if picked:
        detail += f" / 只在 {len(picked)} 台上分发"
    audit(
        db,
        user.username,
        "upload",
        f"{row.owner}/{row.file_key}",
        detail=detail,
    )
    # ★ 阶段耗时随响应一起回去 —— 前端那条进度条的数据来源（见 core/timing.py）。
    out = _file_public(row, mgr)
    out["timings"] = sw.payload()
    metrics.record("upload", sw.total_ms(), sw.rows())
    return out


# ---------------------------------------------------------------------------
# 修改（改块 / 追加 / 截断）
# ---------------------------------------------------------------------------

@router.patch("/{file_id}")
def patch_file(
    file_id: int,
    body: FilePatchIn,
    user: UserRow = Depends(current_user),
    mgr: StoreManager = Depends(get_manager),
    db: Session = Depends(get_db),
):
    """改**已上传**的文件（**只有所有者**）。四个变体用 ``body.op`` 选：

    * ``modify``（默认）：换某一**块**的内容（见 :func:`_do_modify`）；
    * ``zero``：把某一**块**换成**等长的全 0 字节**（见 :func:`_do_zero`）
      —— 它走的是**改块**那条路，不是删除；
    * ``append``：在**末尾新增**内容（见 :func:`_do_append`）；
    * ``truncate``：删掉**末尾**若干块（见 :func:`_do_truncate`）。

    四个变体的共同点：**已有的块一个都不动**（``modify`` / ``zero`` 只动它点名
    的那一块），所以请求代价都只与"动了多少"成正比，与文件本身多大无关。
    """
    row = _file_or_404(db, file_id)
    # ★ 只有所有者能改 —— **管理员也不行**。
    #
    #   这里原来放行 admin（``and user.role != "admin"``）。去掉它是有意的：
    #   管理员**读不到**别人的文件（解密要所有者的私钥），却能**改**，
    #   而且改完所有者那边**没有任何提示**（改块审计只有管理员看得到）。
    #   合起来就是「能写不能读、写完了没人知道」—— 那不是一个干净的授权模型。
    #
    #   收紧之后三条线各自一致：**验证**谁都能做（δ 公开）、
    #   **解密**只有所有者、**修改**也只有所有者。
    if row.owner != user.username:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "只有所有者能改这份文件 —— 改块要重新封装块密钥，"
            "这与上传是同一级别的权限（管理员也不行）。\n"
            "  注意：这不影响验证 —— 这份文件的完整性你照样能验。",
        )

    if body.op == "zero":
        return _do_zero(file_id, body, row, user, mgr, db)
    if body.op == "modify":
        return _do_modify(file_id, body, row, user, mgr, db)
    if body.op == "append":
        return _do_append(file_id, body, row, user, mgr, db)
    return _do_truncate(file_id, body, row, user, mgr, db)


def _do_modify(
    file_id: int,
    body: FilePatchIn,
    row: FileRow,
    user: UserRow,
    mgr: StoreManager,
    db: Session,
):
    """``op=modify``：换一块的内容。

    :param body.data_b64: 新内容的 base64。

    .. important::

       **块密钥会换成新的。** 改块必然重新加密，旧密钥连旧密文一起作废，
       所以必须重新用**所有者的公钥**封一次 —— 否则新内容谁也解不开。
       （这一步不需要任何全局密钥：公钥在 ``users.pub_key`` 里就有。）

    .. note::

       ``content_digest`` **不更新**。见 :func:`_file_public` 的注释。
       响应里的 ``version`` 才是"现在第几版"的权威答案（也是防重放序号）。
    """
    assert body.block_idx is not None  # 由 FilePatchIn._required_fields 保证
    data = body.data()
    _reject_if_pending(mgr)
    try:
        with collect() as sw:
            out = mgr.modify_block(row.owner, row.file_key, body.block_idx, data)
    except NotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except (OutOfRange, Conflict) as exc:
        # 块号越界、内容超过单块上限 —— 都是"请求不对"
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except WriteError as exc:
        raise _write_failed(exc) from exc

    audit(
        db,
        user.username,
        "modify_block",
        f"{row.owner}/{row.file_key}",
        detail=f"第 {body.block_idx} 块 / {len(data)} B → 第 {out['version']} 版",
    )
    # 改库是在另一个 session 里提交的，这里的 ORM 对象已经过期 ——
    # 不 refresh 的话响应里的 version / total_bytes 会退回旧值。
    db.refresh(row)
    metrics.record("modify", sw.total_ms(), sw.rows())
    return {**out, "file": _file_public(row, mgr), "timings": sw.payload()}


def _do_zero(
    file_id: int,
    body: FilePatchIn,
    row: FileRow,
    user: UserRow,
    mgr: StoreManager,
    db: Session,
):
    r"""``op=zero``：把某一块的内容换成**等长的全 0 字节**。

    它走的是**改块**那条路（论文 §8.2 的 ``op = mod``），不是删除：

    * 块还在：下标不变、仍占节点存储、``n`` 不变；
    * 内容变成全 0，块密钥会换成新的（旧密钥连旧密文一起作废），并用
      **所有者的公钥**重新封装 —— 所以**只有所有者**解密时看得到那一串 0；
    * 该位置的承诺分量随之改变，并像改块一样推给持有它的节点；
    * 事后任何人都能**正常验证通过** —— 这是应该的：承诺与密文始终一致。

    .. important::

       **"只把承诺里那个元素置 0"是做不到的**（也不该做到）：验证方不采用
       节点声称的分量，而是把密文拿回来**自己算** :math:`F_i = vector_element(c_i)`
       （见 ``core/store.py`` 开头那条纪律）。所以单方面改承诺或单方面改密文，
       都会在验证那一步被抓到。

    .. note::

       长度用**这一块原来的长度**，不重新切块。所以 ``total_bytes`` 不变，
       连"这块被清过"也不从长度上泄露。
    """
    assert body.block_idx is not None  # 由 FilePatchIn._required_fields 保证
    _reject_if_pending(mgr)
    try:
        with collect() as sw:
            out = mgr.zero_block(row.owner, row.file_key, body.block_idx)
    except NotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except (OutOfRange, Conflict) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except WriteError as exc:
        raise _write_failed(exc) from exc

    audit(
        db,
        user.username,
        "zero_block",
        f"{row.owner}/{row.file_key}",
        detail=(
            f"第 {body.block_idx} 块清零（{out['plain_len']} B 全 0，长度不变）"
            f"→ 第 {out['version']} 版"
        ),
    )
    db.refresh(row)
    # 它本来就是改块，指标跟着 "modify" 记 —— 不另造一个名字去撑大统计口径
    metrics.record("modify", sw.total_ms(), sw.rows())
    return {
        **out,
        "zeroed": {"block_idx": body.block_idx, "bytes": out["plain_len"]},
        "file": _file_public(row, mgr),
        "timings": sw.payload(),
    }


def _do_append(
    file_id: int,
    body: FilePatchIn,
    row: FileRow,
    user: UserRow,
    mgr: StoreManager,
    db: Session,
):
    """``op=append``：在末尾追加内容。

    追加**只加新块**：已有块的密文、密钥、IV、全局下标一个字都不动，
    所以这个请求的代价只与"这次追加了多少"成正比。

    .. note::

       * 追加进去的每一块**单独一把密钥**，各自用**所有者的公钥**封装 ——
         所以所有者在整个文件上都能解密，不会出现“老内容能读、追加的读不了”。
       * 追加会**换新的块密钥**，但**不动**旧块的密钥。
       * ``content_digest`` 与改块同理**不更新**（协调者没有完整明文）。

    .. warning::

       **这个文件的全局下标从此不再保证是一段连续区间。** 两次追加之间若别的
       文件也占了位置，本文件的块就会变成 ``(0, 1, 4)`` 这样带缺口的集合。
       顺序仍是块序（读取拼接正确），但界面上不能再拿 first..last 当区间用 ——
       :func:`_file_public` 给的 ``indices`` 才是权威的那个集合。

    → 新追加上去的块用**同一把所有者公钥**封块密钥，所以所有者在整个文件上
      都能解密，不会出现"前几块能读、后面追加的读不了"。
    """
    data = body.data()
    _reject_if_pending(mgr)
    try:
        with collect() as sw:
            out = mgr.append_to_file(row.owner, row.file_key, data)
    except NotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except (OutOfRange, Conflict) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except WriteError as exc:
        raise _write_failed(exc) from exc

    audit(
        db,
        user.username,
        "append_file",
        f"{row.owner}/{row.file_key}",
        detail=(
            f"追加 {len(data)} B → 新增 {out['added_blocks']} 块"
            f"（下标 {out['indices'][0]}-{out['indices'][-1]}），"
            f"共 {out['block_count']} 块 → 第 {out['version']} 版"
        ),
    )
    db.refresh(row)
    # ★ 这里记的必须是 **append**（安全审计 P5）：以前 append 记成 "truncate"、
    #   truncate 记成 "append"，两个数在性能页上完全颠倒 —— 排查时会误导人。
    metrics.record("append", sw.total_ms(), sw.rows())
    return {**out, "file": _file_public(row, mgr), "timings": sw.payload()}


def _do_truncate(
    file_id: int,
    body: FilePatchIn,
    row: FileRow,
    user: UserRow,
    mgr: StoreManager,
    db: Session,
):
    r"""``op=truncate``：删掉**末尾**若干块。

    它是三个变体里最"窄"的一个 —— 不是实现偷懒，是方案的限制：

    * 底层 ``del`` 只能删全局向量的**末尾连续**区间（新摘要用到
      :math:`e_{[n]} = e_{[n-k]}\cdot e_K` 这个因式分解）；
    * 于是「只有最后写进向量的那份文件才删得动尾巴」；
    * 参与更新的每一台节点，其持有集合必须「要么全含待删区间、要么与它完全
      不相交」。本系统 4 台轮转 + 每块 2 份副本，跨台区间几乎必然部分相交
      —— 所以协调者会**先让那些节点把那部分交回去**（论文的 ``RmvStorage``），
      再发起这次删除。

    .. warning::

       删除是真的丢数据：那些块的密文、以及**封装过的块密钥**都从库里
       删掉，节点侧也真删。所以它不可撤销 —— 界面必须先让用户确认，
       接口这一层不再二次拦截。
    """
    if body.drop_blocks is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "op=truncate 必须给 drop_blocks（要删掉末尾几块）",
        )
    _reject_if_pending(mgr)
    try:
        with collect() as sw:
            out = mgr.truncate_file(row.owner, row.file_key, body.drop_blocks)
    except NotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except (OutOfRange, Conflict) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except WriteError as exc:
        raise _write_failed(exc) from exc
    except TransportError as exc:
        # 交回阶段失败：**没有改动任何全局状态**，而且交回是幂等的，
        # 所以修好那台之后重来一次就是正确修法（不需要补推）。
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc

    audit(
        db,
        user.username,
        "truncate_file",
        f"{row.owner}/{row.file_key}",
        detail=(
            f"删掉末尾 {out['dropped_blocks']} 块"
            f"（全局下标 {out['dropped_indices']}），"
            f"共 {out['block_count']} 块 → 第 {out['version']} 版"
        ),
    )
    db.refresh(row)
    # ★ 同理：这里记的必须是 **truncate**（安全审计 P5）。
    metrics.record("truncate", sw.total_ms(), sw.rows())
    return {**out, "file": _file_public(row, mgr), "timings": sw.payload()}


# ---------------------------------------------------------------------------
# 删除整份文件
# ---------------------------------------------------------------------------


@router.delete("/{file_id}")
def delete_file(
    file_id: int,
    user: UserRow = Depends(current_user),
    mgr: StoreManager = Depends(get_manager),
    db: Session = Depends(get_db),
):
    r"""删掉一份文件（**只有所有者**能做）。

    ★ 新架构下它**只删这份文件自己**：每份文件各占自己的位置段（互不重叠，
    而且位置段**永不回收**），所以"删中间一份要把后面的全删掉"这条束缚
    已经不存在了 —— 连带删除、连带名单、409 都跟着消失：

    * 不会再连累任何别的文件（返回值里的 ``deleted_files`` 正常只有它自己）；
    * 不会再因为"后面压着别人的文件"而回 409；
    * 删除**真的丢数据**（密文、封装过的块密钥、文件账目一起没），
      面板上先让用户确认，这一层不再二次拦截。

    .. important::

       想"只删中间某份文件、把后面的块整体前移"是不行的 —— 那要把剩下的
       位置重编号、重新承诺整条向量，论文里没有这个操作
       （见 ``core/store.py::truncate`` 的说明）。
    """
    row = _file_or_404(db, file_id)
    # ★ 与改块同一条纪律：**只有所有者**。管理员也不行 —— 它能读的只有
    #   审计，而"能删不能读"会变成一个说不清的授权模型。
    if row.owner != user.username:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "只有所有者能删这份文件 —— 它会把这份文件的块（连同之后上传的块）"
            "从全网真的删掉。\n"
            "  注意：这不影响验证 —— 别的文件你照样能验。",
        )
    _reject_if_pending(mgr)
    owner, file_key = row.owner, row.file_key
    try:
        with collect() as sw:
            out = mgr.delete_file(owner, file_key)
    except NotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except Conflict as exc:
        # 「后面还压着别人的文件」走这条：409 而不是 400 —— 那不是参数写错，
        # 而是"当前状态不允许"（换个顺序就能做）。
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except OutOfRange as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except WriteError as exc:
        raise _write_failed(exc) from exc
    except TransportError as exc:
        # 交回阶段失败：没有改动任何全局状态，交回是幂等的 ⇒ 重来一次即可
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc

    extra = [f for f in out["deleted_files"] if f != f"{owner}/{file_key}"]
    audit(
        db,
        user.username,
        "delete_file",
        f"{owner}/{file_key}",
        detail=(
            f"删掉 {out['dropped_blocks']} 块（全局下标 {out['dropped_indices']}）"
            + (f"，连带 {len(extra)} 份文件：{'、'.join(extra[:5])}" if extra else "")
            + f"；删后向量 n = {out['blocks_after']}"
        ),
    )
    metrics.record("delete", sw.total_ms(), sw.rows())
    return {**out, "timings": sw.payload()}


# ---------------------------------------------------------------------------
# 列表 / 详情（验证不受限，所以不按所有者过滤）
# ---------------------------------------------------------------------------

@router.get("")
def list_files(
    user: UserRow = Depends(current_user),
    mgr: StoreManager = Depends(get_manager),
    db: Session = Depends(get_db),
):
    """**所有**文件 —— 验证不受限，所以不按所有者过滤。

    每行额外给出"我能不能解密"，让前端用两把不同的锁显示两条线。
    """
    rows = db.execute(select(FileRow).order_by(FileRow.id)).scalars().all()
    out = []
    for row in rows:
        item = _file_public(row, mgr)
        item["can_decrypt"] = can_decrypt(user, row)
        item["is_mine"] = row.owner == user.username
        out.append(item)
    return out


@router.get("/{file_id}")
def file_detail(
    file_id: int,
    elements: int = 0,
    user: UserRow = Depends(current_user),
    mgr: StoreManager = Depends(get_manager),
    db: Session = Depends(get_db),
):
    """文件详情 —— 含每一块的账目（下标 / 持有者 / 副本 / 明文长度）。

    :param elements: 传 1 时，每一块额外带上 ``element``：该块的**公开分量**
        （群元素，十进制字符串）。界面上的「详细」模式用它显示"这一块的指纹"，
        悬浮看完整十六进制。

        ★ 为什么默认**不给**：1024 块的文件会凭空多出三百多 KB 的响应，
          而「简略」模式根本不显示这些数。
        ★ 给这个量**不构成新的泄露**：分量本来就是公开的（"验证不受限"，
          ``/api/query`` 把它给任何登录用户）—— 验证者比的就是它。
          反过来说，块密钥密文（``key_ct``）**任何模式都不给**，
          那才是"只有所有者能解密"的根。
    """
    row = _file_or_404(db, file_id)
    item = _file_public(row, mgr)
    item["can_decrypt"] = can_decrypt(user, row)
    item["is_mine"] = row.owner == user.username
    blocks = list(
        db.execute(
            select(BlockRow)
            .where(BlockRow.file_id == file_id)
            .order_by(BlockRow.block_idx)
        ).scalars()
    )
    item["layout"] = [
        {
            "global_index": b.global_index,
            "block_idx": b.block_idx,
            "holder": b.holder,
            # 全部持有者（主副本在前）。旧数据是 "[]" → 退化成"只有 holder 一份"，
            # 所以界面上"这一块存在哪几台"在开副本之前传的文件上也不会显示错。
            "replicas": json.loads(b.replicas or "[]") or [b.holder],
            "plain_len": b.plain_len,
            **({"element": b.element} if elements else {}),
        }
        for b in blocks
    ]
    return item


# ---------------------------------------------------------------------------
# 客户端自解密（服务器不可信）
# ---------------------------------------------------------------------------

@router.post("/{file_id}/replay")
def replay_route(
    file_id: int,
    body: ReplayIn,
    user: UserRow = Depends(current_user),
    mgr: StoreManager = Depends(get_manager),
    db: Session = Depends(get_db),
):
    r"""**回滚演示**：让服务器对某一块交回「旧版本」（演示“服务器不可信”）。

    ★ 这不是“业务功能”，是**演示工具** —— 把“服务器不可信”这句话变成看得见
      的一幕：打开它，服务器就**真的**开始对这块交回旧密文（后续 ``/cipher``
      里换掉），客户端自己算出的分量对不上当前基准，于是**验证不通过**。

    ★ 演示顺序（重要）：**先**打开（存下当前这版）→ **再**改块 → **然后**解密。
      这样服务器交回的正好是“改之前那一版”。关掉就恢复正常。

    ★ 只有**所有者**（或管理员）能开：这是对自己的文件做演示。
      开关本身进审计流水 —— 演示归演示，动作要留痕。
    """
    row = db.get(FileRow, file_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "文件不存在")
    if row.owner != user.username and user.role != "admin":
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "只有文件所有者能对这份文件做回滚演示",
        )
    try:
        out = mgr.replay_arm(row.owner, row.file_key, body.block_idx, on=body.on)
    except Exception as exc:  # noqa: BLE001 - 文件/块不存在或越界，都归 400
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    audit(
        db,
        user.username,
        "replay_arm" if body.on else "replay_disarm",
        f"{row.owner}/{row.file_key} 第 {body.block_idx} 块",
        detail=(
            "打开回滚演示：/cipher 对这一块将交回存下来的旧版本"
            if body.on
            else "关闭回滚演示：恢复交回当前版本"
        ),
    )
    return out


@router.post("/{file_id}/cipher")
def cipher_route(
    file_id: int,
    body: DecryptIn,
    with_primes: bool = True,
    user: UserRow = Depends(current_user),
    mgr: StoreManager = Depends(get_manager),
    db: Session = Depends(get_db),
):
    """**取密文（不解密）** —— 浏览器自解密 + 自验证那条路的入口。

    与 :func:`decrypt` 的差别只有一句话，但它是整个安全模型的分界：

    * ``/decrypt``：服务端拿会话私钥把内容解开，再把**明文**给你。
      于是你拿到的东西**无法被你自己检验** —— 服务器给什么就是什么；
    * ``/cipher``：服务端只交出**材料** —— 密文段、IV、明文长度、
      块密钥的 SM2 密文、一份证据，以及公开参数（N / g / 素数）。
      块密钥的解封与内容解密都在**浏览器**里做，
      而且密文→分量→承诺这条链每一步都能被浏览器独立重算。

    服务端在这条路上**不掌握任何秘密**（会话私钥不再参与），
    它唯一能作恶的地方是"给一份过不了验证的应答" —— 而那会被当场拒绝。

    ★ 权限与 ``/decrypt`` 一致（只有所有者）—— 但这**不是**"别人拿不到密文"：
      密文本就存在不可信节点上，任何登录用户都能通过 ``/api/query`` 拿到
      它对应的**分量**（验证不受限）。限制在这条路上只是"别让界面把
      块密钥密文当成可读的东西散出去"。
    """
    row = _file_or_404(db, file_id)

    if not can_decrypt(user, row):
        audit(
            db,
            user.username,
            "cipher_denied",
            f"{row.owner}/{row.file_key}",
            ok=False,
            detail="不是所有者",
        )
        raise HTTPException(status.HTTP_403_FORBIDDEN, DENIED_NOT_OWNER)

    try:
        with collect() as sw:
            out = mgr.cipher_pack(
                row.owner, row.file_key, body.indices, with_primes=with_primes
            )
    except NotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except OutOfRange as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    audit(
        db,
        user.username,
        "cipher",
        f"{row.owner}/{row.file_key}",
        detail=f"{len(out['blocks'])} 块密文（未解密）",
    )
    out["file_id"] = row.id
    out["timings"] = sw.payload()
    return out


# ---------------------------------------------------------------------------
# 解密（受限）
# ---------------------------------------------------------------------------

@router.post("/{file_id}/decrypt")
def decrypt(
    file_id: int,
    body: DecryptIn,
    user: UserRow = Depends(current_user),
    token: str = Depends(current_token),
    mgr: StoreManager = Depends(get_manager),
    db: Session = Depends(get_db),
):
    """**解密受限**：只有所有者解得开 —— 但这**不影响验证**。

    被拒绝时返回的提示刻意写清楚：你照样能验证这些块的完整性，
    只是读不到明文。

    ★ 这里有两道门，它们**不是重复**，各管一件事：

      ① 应用层（下面那句 ``can_decrypt``）：不是你的就不是你的，
         提前拒、写审计、给一句人话。零密码学开销。

      ② 密码学（``mgr.read_plain``）：**你这把私钥解不解得开这段密文**。
         没有会话私钥 → 403 并叫你重新登录；解不开 → :class:`DecryptDenied`。

      只留 ① 的话，绕过接口层直接读库就能拿到明文；只留 ② 的话，
      用户看到的是"算不出来"而不知道为什么。
    """
    row = _file_or_404(db, file_id)

    if not can_decrypt(user, row):
        audit(
            db,
            user.username,
            "decrypt_denied",
            f"{row.owner}/{row.file_key}",
            ok=False,
            detail="不是所有者",
        )
        raise HTTPException(status.HTTP_403_FORBIDDEN, DENIED_NOT_OWNER)

    # ★ 解密能力来自**本次登录解封出来的私钥**，不是从库里现查的。
    #   没有它就没法解密 —— 这是"口令封装私钥"的必然结果，也正是它想要的效果。
    sk = mgr.session_key_of(token)
    if sk is None:
        audit(
            db,
            user.username,
            "decrypt_denied",
            f"{row.owner}/{row.file_key}",
            ok=False,
            detail="会话里没有私钥",
        )
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "服务端这次会话里没有你的私钥 —— 这是**默认模型**：\n"
            "  私钥在浏览器里用口令解封（登录响应里的 key_blob），"
            "后端不解封、也不保存。\n"
            "  要解密请走客户端路径：POST /api/files/{id}/cipher 取密文，"
            "由浏览器解封块密钥并 SM4 解密。\n"
            "  （想对比旧模型：登录时显式传 server_key=true。）",
        )

    try:
        with collect() as sw:
            out = mgr.read_plain(row.owner, row.file_key, sk, body.indices)
    except DecryptDenied as exc:
        # ★ 第②道门真正落地的地方：**真的没解开**。
        audit(
            db,
            user.username,
            "decrypt_denied",
            f"{row.owner}/{row.file_key}",
            ok=False,
            detail=f"密码学层拒绝：{exc}",
        )
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"{exc}\n注意：这不影响验证 —— 那些块你照样可以验证完整性。",
        ) from exc
    except NotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except OutOfRange as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    audit(
        db,
        user.username,
        "decrypt",
        f"{row.owner}/{row.file_key}",
        detail=f"{out['bytes']} B",
    )
    out["file_id"] = row.id
    # ★ 阶段耗时：定位持有者 / 取回密文 / 解封块密钥（ECIES） / 解密（SM4） / 拼接
    out["timings"] = sw.payload()
    return out


# 说明：原先这里还有一整套"可控共享"（``POST/DELETE /api/files/{id}/grants``、
# ``_granted_names``、``_sync_policy``）。
#
# 它整个删掉了，而不是保留一个空接口：
#
#   * 那套东西的每一步（用 msk 解出块密钥 → 按新策略重封）都依赖 ABE；
#   * 而现在块密钥是拿**所有者的公钥**封的，想再多封一份给某人，
#     就意味着得再多存一份块密钥密文 —— 那是一套新的数据结构，
#     不是“保留接口”能顶上。
#
# 于是现在的语义很硬：**只有所有者能解密**。连管理员也不行 ——
# 没有任何人能解别人的文件，因为没有人拥有那把私钥。
# （要恢复共享，正确的做法是给 ``blocks`` 加一张"额外封装"表，
#   而不是回到 ABE。）
