"""VDS 系统核心层 —— 每份文件一条独立向量。

与 ``svc`` / ``vds`` 的关系
--------------------------
``svc`` 是向量承诺本体（论文 §5.2），``vds`` 是"一个文件 = 一条向量"的
可验证分布式存储（§8.2）。本层把这两层接到真实运行状态上：分块加密、
位置登记、节点状态机、密钥封装与传输抽象。

本层的向量组织（改造后）
------------------------
每份文件**各自**一条向量、各自一份摘要 δ：

    * 新建或改动一份文件，**完全不触碰**别人的摘要；
    * 位置由块的身份派生（:class:`IdentityPrimeView`：``H(owner‖file_key‖i)``
      → 素数），所以位置不需要全局分配、段永不回收；
    * 跨文件取证要在**合并位置集**上重算一份证据与合并承诺（算法核要求
      同一份文件内的下标两两不交），代价是秒级 —— 换来的是上面那条隔离性。

代价（必须知道）
----------------
1. **``n`` 是"全局已用位置数"**（各文件块数之和，不含历史空洞），
   不是任何一份文件的长度；"文件"这个概念只存在于登记表
   （:mod:`core.registry`）里。
2. **一次上传只推进该文件的 δ**，但要把新状态同步给持有其分块的节点
   （每块另有一份副本落在不同机器上）。论文 §8.2 的两段式更新
   （``PushUpdate`` / ``ApplyUpdate``）正是这条路的形态，
   本层直接复用 :func:`vds.updates.update_append`。
3. **删除按该文件自己的位置集合做**（``VectorStore.delete_from``），
   向量里留下空洞、段永不回收，其余文件一个字节都不动。

三条安全红线（规划 §6.4）
-----------------------
* ``N``、``g`` 公开；**``p``、``q``、``φ(N)`` 绝不落库**；
* RNG 种子必须随机（``None``），否则知道种子的人能重跑出 ``p``、``q``；
* 日志与 API 响应不带生成过程的中间量。

分层
----
::

    VectorStore（协调者）──持有──> 各文件的向量与摘要、登记表、分片决策
          │
          └── NodeTransport ──> NodeState × k（每台存储节点的本地状态）
                  │                    └── 自己那段的密文
                  ├─ LocalTransport  同进程对象（单进程演示 / 测试）
                  └─ HttpTransport   独立进程 + 各自 SQLite（真分布式）

**协调者不持有任何节点状态，也不替节点算新状态。** 追加时它只广播
"发生了什么"（Δ 与 Υ_Δ）以及"你负责哪些新位置"，各节点自己算出来。
这是两段式更新的要点，也是 :mod:`core.transport` 这个抽象存在的意义。
"""

from __future__ import annotations

from .crypto import (
    DIGEST_BYTES,
    L,
    SEGMENT_BYTES,
    decrypt_segment,
    encrypt_segment,
    new_key,
    vector_element,
)
from .node_state import NodeRejected, NodeState
from .registry import BlockRef
from .session import (
    DEFAULT_L,
    DEFAULT_MODULUS_BITS,
    DEFAULT_N_MAX,
    GlobalSession,
    crs_from_dict,
    crs_to_dict,
    get_primegen,
    make_crs,
    new_session,
)
from .store import FileRecord, QueryResult, VectorStore
from .transport import LocalTransport, NodeTransport, TransportError

__all__ = [
    # session
    "GlobalSession",
    "new_session",
    "make_crs",
    "get_primegen",
    "crs_to_dict",
    "crs_from_dict",
    "DEFAULT_L",
    "DEFAULT_N_MAX",
    "DEFAULT_MODULUS_BITS",
    # registry
    "BlockRef",
    # crypto
    "SEGMENT_BYTES",
    "DIGEST_BYTES",
    "L",
    "new_key",
    "encrypt_segment",
    "decrypt_segment",
    "vector_element",
    # store
    "VectorStore",
    "FileRecord",
    "QueryResult",
    # node
    "NodeState",
    "NodeRejected",
    "NodeTransport",
    "LocalTransport",
    "TransportError",
]
