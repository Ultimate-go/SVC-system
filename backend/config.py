"""后端配置。

所有可调参数集中在这里，测试能造一份指向临时库的配置，不去碰真实数据。
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["Settings", "BASE_DIR", "default_settings"]

BASE_DIR = Path(__file__).resolve().parent.parent


def _node_ids_from_env() -> tuple[str, ...]:
    raw = os.environ.get("VDS_NODE_IDS", "").strip()
    if not raw:
        return ("node-1", "node-2", "node-3", "node-4")
    return tuple(x.strip() for x in raw.split(",") if x.strip())


def _node_urls_from_env() -> dict[str, str] | None:
    """从 ``VDS_NODE_URLS`` 解析 ``node-1=http://127.0.0.1:9101,node-2=...``。

    没设这个环境变量 = 单进程模式（节点是进程内对象），
    设了 = 真分布式模式（节点是独立进程）。两种模式共用同一份
    :class:`~core.node_state.NodeState` 逻辑，所以切换不影响任何代数结果。
    """
    raw = os.environ.get("VDS_NODE_URLS", "").strip()
    if not raw:
        return None
    out: dict[str, str] = {}
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise ValueError(f"VDS_NODE_URLS 里这一段缺 '='：{chunk!r}")
        nid, url = chunk.split("=", 1)
        nid, url = nid.strip(), url.strip()
        if not nid or not url:
            raise ValueError(f"VDS_NODE_URLS 这一段不完整：{chunk!r}")
        if nid in out:
            raise ValueError(f"VDS_NODE_URLS 里节点 {nid} 出现了两次")
        out[nid] = url
    return out or None


@dataclass
class Settings:
    """一次后端实例的全部配置。

    :param db_path: SQLite 文件路径
    :param secret_key: JWT 签名密钥。默认从环境变量 ``VDS_SECRET_KEY`` 取，
        没有再随机生成一个 —— **随机生成意味着重启后旧 token 全失效**，
        演示够用，生产必须固定注入。
    :param modulus_bits: ``|N|``。**演示档 1024，无安全强度**；论文配置 16λ = 2048
    :param l: 每个分量的比特数。256 正好装一个 SM3 摘要
    :param n_max: 全局位置上限。**一旦定死不可改**（隐藏阶群的可用位置
        在 Bootstrap 阶段就定死）
    :param segment_bytes: 一个向量分量对应多少字节密文
    :param replica_factor: 每块存**几份**（默认 2）。

        **副本不占全局位置** —— ``n`` 仍然等于块数，不乘副本数；
        所以“一次上传 = 一次全网事件”这个代价不会因副本翻倍。
        多出来的是各节点自己的磁盘占用。

        为什么默认开 2 份：默认 1 份时“删掉一台节点的数据目录，
        它负责的那些块就真的没了”，而多副本正好把这个洞堵上。
        注意它不影响**承诺与证据**的大小，也不需要改 ``n_max``。
    :param node_ids: 存储服务器标识
    :param node_urls: 各节点的 HTTP 地址。**为 None 时是单进程模式**（节点在
        同一个进程里），设了就是真分布式模式。两种模式的代数结果逐位相同，
        有测试钉住这一点。
    :param node_token: 节点令牌（``VDS_NODE_TOKEN``）。节点一律要求鉴权，
        否则 ``/node/reset``（破坏性）与 ``/node/retrieve``（会吐出该节点
        持有的全部密文）就只是靠"绑回环"护着，而绑哪个地址是部署参数。
        **单进程模式用不到它**（没有 HTTP）；跨进程模式必须设，
        而且必须与节点启动时的一致。
    :param crs_seed: **必须是 None**。固定种子 = 知道种子的人能重跑出 ``p``、``q``
        = 等价于泄露 ``φ(N)`` = 能伪造任意证据（规划 §6.4 红线 2）。
        这个字段存在的唯一目的是让测试能显式传入；生产路径不传。
    :param token_ttl_minutes: JWT 有效期

    关于**解密密钥**（原先这里是 ``abe_msk_path`` / ``abe_scheme`` 两项）
    ----------------------------------------------------------------------
    删掉 ABE 之后，密钥材料不再有"一份全局主密钥"这种东西：

    * 每个用户一对 SM2 密钥；
    * **公钥**存在 ``users.pub_key``（公开，本来就无需保护）；
    * **私钥**用**用户自己的登录口令**包一层，密文存在 ``users.sk_wrapped``；
      登录时解封、只放在后端内存的会话里，**从不落盘**。

    所以这里没有任何"密钥文件路径"可配 —— 也就没有了"测试库配错\n    到开发库那份主密钥"那个坑（真踩过）。
    """

    db_path: Path = field(default_factory=lambda: BASE_DIR / "vds.db")
    secret_key: str = field(
        default_factory=lambda: os.environ.get("VDS_SECRET_KEY") or secrets.token_urlsafe(32)
    )
    modulus_bits: int = 1024
    l: int = 256
    n_max: int = 1024
    segment_bytes: int = 1024
    #: 上传时允许选的块大小范围（字节）—— :attr:`segment_bytes` 是**默认值**，
    #: 这两个是**允许范围**。前端给几个档，服务端在这里兜底校验。
    #:
    #: 为什么要给下界：块越小块数越多，而位置上限 ``n_max`` 是先定死的 ——
    #: 1 字节一块能把任何文件都撞上 ``n_max``，而那个报错看起来像“文件太大”，
    #: 不像是“块选小了”。上界则是防一手“一块 1 GB”这种把内存吃光的选法。
    segment_bytes_min: int = 64
    segment_bytes_max: int = 1 << 20  # 1 MiB

    #: 与节点说话时的**退避重试**次数与退避基数（秒）。
    #:
    #: 为什么需要：一次上传/改块/追加要给每一台发一遍，任何一台在那几秒里
    #: 重启就会让**整次操作**失败。这类失败绝大多数是瞬态的。
    #: 测试里可以设 0 来把随机延迟消掉。
    node_retries: int = 2
    node_retry_backoff: float = 0.1
    #: 每块存几份。**副本不占全局位置**（见类文档）：n 仍然是块数。
    replica_factor: int = 2
    node_ids: tuple[str, ...] = field(default_factory=_node_ids_from_env)
    node_urls: dict[str, str] | None = field(default_factory=_node_urls_from_env)
    node_token: str = field(default_factory=lambda: os.environ.get("VDS_NODE_TOKEN", ""))
    crs_seed: bytes | None = None
    token_ttl_minutes: int = 12 * 60
    debug: bool = False

    #: 写失败之后的**自动补推**（定时 + 次数上限）。
    #:
    #: ★ 间隔默认 5 秒是故意的：既要给“人工看到报警、先看看怎么回事”留出时间，
    #:   也要让“必须看到 409 拦住下一次写”那类断言/演示步骤不被后台线程抢跑。
    #: ★ 次数**有上限**也是故意的：无限重试会把“哪台一直不在线”这件事藏起来。
    #:   用完就停在原地，待补推视图里会明说“已自动重试 N 次仍未成功，请人工处理”。
    #:   人工在集群页点一次「补推」会把计数重新给满（见 `.manager.retry_push`）。
    retry_auto: bool = True
    retry_interval_s: float = 5.0
    retry_max_attempts: int = 5

    def database_url(self) -> str:
        return f"sqlite:///{self.db_path.as_posix()}"

    @property
    def distributed(self) -> bool:
        """节点是不是独立进程。"""
        return bool(self.node_urls)

    def check_node_config(self) -> None:
        """跨进程模式下，节点名单与地址必须一一对应。

        不一致是最容易犯的错（举例：改了一半配置），而后果是“少了一个
        分片但没人发现”，分片表与节点实际持有量对不上。宁可启动时就报错。
        """
        if not self.node_urls:
            return
        missing = sorted(set(self.node_ids) - set(self.node_urls))
        extra = sorted(set(self.node_urls) - set(self.node_ids))
        if missing or extra:
            raise ValueError(
                "VDS_NODE_IDS 与 VDS_NODE_URLS 不一致"
                f"（node_ids 多出 {missing}，node_urls 多出 {extra}）"
                "—— 两个变量必须列出同样的节点"
            )


def default_settings() -> Settings:
    return Settings()
