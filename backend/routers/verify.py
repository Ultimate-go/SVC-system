"""验证路由 —— **验证不受限**。

这里的所有接口**不做任何权限判断**，因为按设计决定（规划 §0.0 决策 2），
验证是公开能力：:math:`\\delta` 公开，任何登录用户都能验证任意位置的完整性。
这与题目原文一致 —— "系统中所有用户都能够验证分布式存储数据任意部分的完整性"。

返回的是**向量分量（密文摘要）+ 一份聚合证据 + 验证结论**，不是明文。
要明文得走 ``/api/files/{id}/decrypt``，那条线才受限。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from .. import metrics
from ..deps import audit, current_user, get_db, get_manager
from ..manager import NotFound, OutOfRange, StoreManager
from ..models import UserRow
from ..schemas import BatchVerifyIn, DisaggIn, FileQueryIn, QueryIn
from core.timing import collect

router = APIRouter(prefix="/api", tags=["verify"])


@router.post("/query")
def query_by_indices(
    body: QueryIn,
    user: UserRow = Depends(current_user),
    mgr: StoreManager = Depends(get_manager),
    db: Session = Depends(get_db),
):
    """按**全局下标**查询。一次可以横跨多个文件、多个用户 —— 这就是设计 B。

    ``allow_partial=False``（默认）时有块收不齐就 400，并在报错里列清楚缺哪几个
    下标、各台各持有多少块；开了它则只把**拿得到的**算进结论，
    缺的下标放在响应的 ``missing`` 里（前端必须显著显示）。
    """
    try:
        with collect() as sw:
            out = mgr.query(body.indices, allow_partial=body.allow_partial)
    except OutOfRange as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    audit(
        db,
        user.username,
        "query",
        f"{len(body.indices)} 块" + ("（允许部分）" if body.allow_partial else ""),
        ok=out["ok"],
        detail=(out["verify"]["message"] if not out["ok"] else ""),
    )
    # ★ 阶段耗时：探测各节点 / 取回分量与凭证 / 聚合凭证 / 承诺验证
    out["timings"] = sw.payload()
    metrics.record("query", sw.total_ms(), sw.rows())
    return out


@router.post("/query/files")
def query_by_files(
    body: FileQueryIn,
    user: UserRow = Depends(current_user),
    mgr: StoreManager = Depends(get_manager),
    db: Session = Depends(get_db),
):
    """一次查询**若干个文件**（可跨用户）—— 聚合成**一份**证据。

    在"一文件一向量"的设计下这件事做不到：``svc.agg`` 的前提是两份证据
    属于同一条向量。设计 B 把它们放进同一条，于是天然成立。
    """
    try:
        with collect() as sw:
            out = mgr.query_files(body.targets, body.block_indices)
    except NotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except OutOfRange as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    audit(
        db,
        user.username,
        "query_files",
        f"{len(body.targets)} 个文件 / {len(out['indices'])} 块",
        ok=out["ok"],
    )
    out["timings"] = sw.payload()
    metrics.record("query_files", sw.total_ms(), sw.rows())
    return out


@router.post("/query/verify-batch")
def verify_batch_route(
    body: BatchVerifyIn,
    user: UserRow = Depends(current_user),
    mgr: StoreManager = Depends(get_manager),
    db: Session = Depends(get_db),
):
    """**批量验证**：一次验池子里的多份证据（可跨文件、跨用户）。

    与 ``/api/query`` 一样**不做任何权限判断** —— "验证不受限"这条同样适用：
    谁都能拿自己池子里的证据来验，验得动验不动与能不能解密无关。

    ⚠ **它比逐份验慢**（实测，见规划 §13.6），响应里会把"批量 vs 逐份"
    两个耗时一起返回 —— 界面上**两个都显示**，不藏着。
    真正的价值是"一次结论"这件事本身，以及答辩上把随机系数那条讲清楚。
    """
    try:
        with collect() as sw:
            out = mgr.verify_batch(
                [c.model_dump() for c in body.items],
                locate=body.locate,
                compare=body.compare,
            )
    except OutOfRange as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    audit(
        db,
        user.username,
        "verify_batch",
        f"{out['n']} 份证据",
        ok=out["ok"],
        detail=out["message"] if not out["ok"] else f"{out['ms_batch']} ms",
    )
    out["timings"] = sw.payload()
    metrics.record("verify_batch", sw.total_ms(), sw.rows())
    return out


@router.post("/evidence/disagg")
def evidence_disagg(
    body: DisaggIn,
    user: UserRow = Depends(current_user),
    mgr: StoreManager = Depends(get_manager),
    db: Session = Depends(get_db),
):
    r"""**分解**：从一份已有证据里拆出子集的证据（**不向节点取数据**）。

    这是需求 5「分解再聚合」的前半段。它为何必要，看规划 §3.2：

        :math:`\pi_{1,3,7}` 能拆出 :math:`\pi_{1,3}`、:math:`\pi_7`、
        :math:`\pi_1` … —— 但**都只能是子集**。
        想要 :math:`\pi_{1,3,5}`（``5 ∉ {1,3,7}``）光靠这一份永远拆不出来，
        必须另外取一份**含 5** 的，再聚合。

    所以界面上那条流程是 **4 步**：池子里有 :math:`\pi_{1,3,7}`
    → 拆出 :math:`\pi_{1,3}` → 补一份 :math:`\pi_5` →
    聚成 :math:`\pi_{1,3,5}`。**第三步不能省**，这正是证据池存在的理由。

    返回体与 ``/api/query`` **同构**，前端可以把结果当成一张新卡片直接收进池子。
    """
    try:
        with collect() as sw:
            out = mgr.disagg(
                {
                    "I": body.I,
                    "values": body.values,
                    "S_I": body.S_I,
                    "Lambda_I": body.Lambda_I,
                },
                body.K,
            )
    except OutOfRange as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    audit(
        db,
        user.username,
        "disagg",
        f"{len(body.I)} 块 → {len(out['indices'])} 块",
        ok=out["ok"],
        detail="" if out["ok"] else out["verify"]["message"],
    )
    out["timings"] = sw.payload()
    metrics.record("disagg", sw.total_ms(), sw.rows())
    return out


@router.get("/registry/{global_index}")
def registry_info(
    global_index: int,
    _: UserRow = Depends(current_user),
    mgr: StoreManager = Depends(get_manager),
):
    """全局下标 → "这是谁的第几块、存在哪台服务器"。

    登记表是设计 B 的基础设施：索引必须全系统唯一，而
    ``(用户, 文件, 块序号)`` 三元组的哈希只能当**查表的键**，
    不能取模当索引（会碰撞，而碰撞会让 ``shamir_trick`` 静默算错）。
    """
    try:
        return mgr.registry_info(global_index)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
