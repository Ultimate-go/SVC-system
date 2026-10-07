"""后端配置。

所有可调参数集中在这里，测试能造一份指向临时库的配置，不去碰真实数据。
"""

from __future__ import annotations

import json
import os
import secrets
import socket
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

__all__ = [
    "DeployPorts",
    "Settings",
    "BASE_DIR",
    "DEFAULT_BACKEND_PORT",
    "DEFAULT_FRONTEND_PORT",
    "DEFAULT_NODE_BASE_PORT",
    "DEFAULT_NODE_COUNT",
    "DEPLOY_FILE",
    "MAX_NODE_COUNT",
    "MAX_PORT",
    "MIN_NODE_COUNT",
    "MIN_PORT",
    "check_port_conflicts",
    "clamp_node_count",
    "default_settings",
    "effective_node_ids",
    "effective_ports",
    "node_ports_for",
    "node_urls_for",
    "parse_node_urls",
    "port_in_use",
    "read_deploy_info",
    "read_deploy_node_count",
    "read_deploy_ports",
    "read_deploy_stop_ports",
    "reset_deploy_node_count",
    "reset_deploy_ports",
    "write_deploy_node_count",
    "write_deploy_ports",
]

BASE_DIR = Path(__file__).resolve().parent.parent

#: 默认节点台数。**只在一处写死**（以前这个 4 散落在 config / start_all / run_nodes 里）。
DEFAULT_NODE_COUNT = 4

#: 台数下界。1 台在代数上跑得通，但 ``replica_factor = 2`` 时会被
#: :class:`~core.store.VectorStore` 拒掉（副本必须落在不同的机器上）——
#: 这里不替它做决定，把下界留给 1，让那条更准确的报错自己说出来。
MIN_NODE_COUNT = 1

#: 台数上限。与 ``scripts/start_all.py`` 的 ``STOP_PORT_SPAN = 64``（端口扫
#: 9101-9164）配套 —— 超过它「停不干净」那条兜底就失效了。
MAX_NODE_COUNT = 32


def clamp_node_count(count: int) -> int:
    """把台数夹到 ``[MIN_NODE_COUNT, MAX_NODE_COUNT]``。

    **不抛异常**：这个值来自管理员界面的输入框，越界只该「夹一下并告诉
    界面夹过了」（:func:`read_deploy_node_count` 同样不抛），
    因为一个手抖多打一位不该让整条保存路径报 500。
    真正要拒绝的是 ``write_deploy_node_count`` —— 它写的是文件，
    写进去一个不合法值会让下次启动静默退回默认台数，那才必须拦。
    """
    try:
        n = int(count)
    except (TypeError, ValueError):
        return DEFAULT_NODE_COUNT
    return max(MIN_NODE_COUNT, min(MAX_NODE_COUNT, n))

#: 部署配置：**节点台数**存在这里，改它 = 改部署规模（重启后生效）。
#:
#: ★ 为什么放 ``nodes/`` 而不是库里：``reset_data()`` 会把 ``vds.db`` 和
#:   ``nodes/node-*`` 一起删掉。台数如果存在库里，每重置一次就被打回默认值
#:   —— 而用户明确要的恰恰相反：**重置时主动把它复位成 4**（那是显式行为，
#:   不是"被顺手删掉"）。和 ``nodes/token.txt`` 放一起，语义一致。
DEPLOY_FILE: Path = BASE_DIR / "nodes" / "deploy.json"


def _write_deploy(payload: dict[str, object]) -> None:
    """原子地把**整份**部署配置写下去（先写临时文件，再替换）。

    ★ 为什么非要原子写：写到一半断电/被杀会留下**半截 JSON**，而读它的那一侧
      （:func:`read_deploy_node_count` / :func:`read_deploy_ports`）一律
      "读不动就回默认值" —— 于是下次启动会**静默**按默认值起。
      那种"配置莫名其妙没生效"的故障，比直接报错难查得多。

    ★ 参数是**整份**，含这次没改的键：台数与端口住在同一个文件里
      （见 :data:`DEPLOY_FILE`），各写各的就必须先把对方的键读出来带上 ——
      见 :func:`write_deploy_node_count` 与 :func:`write_deploy_ports` 里那句
      ``dict(read_deploy_info())``。
    """
    DEPLOY_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = DEPLOY_FILE.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    tmp.replace(DEPLOY_FILE)


def read_deploy_info() -> dict:
    """读**整份**部署配置。文件不存在 / 读不动 / 不是个对象 → 空字典。

    为什么不只读 ``node_count``：改台数是"人做的决定"，而界面需要回答
    「上次从几台改到几台、搬了多少块」—— 那几行也记在这个文件里，
    理由与台数本身一样（见 :data:`DEPLOY_FILE` 的注释）：
    它必须活得比任何一个进程久，而 ``--reset`` 删除库时不该顺手抹掉它。

    ★ 用 ``utf-8-sig`` 而不是 ``utf-8``：这个文件是**给人手改的**，而记事本
      之类的编辑器可能给它加一个 BOM，``json.loads`` 见到 BOM 会直接报错。
      （真踩到过：手上写了一份带 BOM 的配置，于是整份配置被当成"读不动"，
      台数与端口**一起**静默退回默认值 —— 表现是"我改的端口自己变回去了"。）
      ``utf-8-sig`` 在没有 BOM 时与 ``utf-8`` 完全一样，所以两边都对。
    """
    try:
        raw = json.loads(DEPLOY_FILE.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def read_deploy_node_count(default: int = DEFAULT_NODE_COUNT) -> int:
    """读部署的节点台数。文件不存在 / 读不动 / 值不合法 → 回默认值。

    **绝不抛异常**：这个文件是给人手改的，写坏一个字符不该让整个后端起不来。
    """
    try:
        n = int(read_deploy_info()["node_count"])
    except Exception:
        return int(default)
    if not (MIN_NODE_COUNT <= n <= MAX_NODE_COUNT):
        return int(default)
    return n


def write_deploy_node_count(
    count: int,
    *,
    prev: int | None = None,
    note: str | None = None,
) -> None:
    """把台数写进部署配置。**下次重启生效**（进程已经按旧台数起来了）。

    :param prev: 改**之前**跑着几台。界面用它拼「4 → 2 台」这句话。
        ``None`` = 这次不是一次"改动"（比如 ``--reset`` 归位），
        于是把上一次的记录一起抹掉 —— 重置之后不该再显示旧的变更提示。
    :param note: 给界面看的一整句话（缩容时那句「文件块已重新分配」）。
    """
    n = int(count)
    if not (MIN_NODE_COUNT <= n <= MAX_NODE_COUNT):
        raise ValueError(
            f"节点台数必须在 {MIN_NODE_COUNT}..{MAX_NODE_COUNT} 之间，收到 {count}"
        )
    # ★ 从现有的整份配置出发，而不是从空字典 —— 端口（``ports`` /
    #   ``stop_ports``）与台数住在同一个文件里，改台数时把端口写没了，
    #   下次启动就会静默把端口退回默认值（表现是"我在界面上改的端口自己变回去了"）。
    payload: dict[str, object] = dict(read_deploy_info())
    # 上一次的变更记录先清掉：换了台数（或复位）之后，关于上一轮的那几句话
    # 就不该再出现。★ 只清这三项，端口那两把键必须原样留着。
    for key in ("prev_count", "changed_at", "note"):
        payload.pop(key, None)
    payload["node_count"] = n
    if prev is not None:
        payload["prev_count"] = int(prev)
        payload["changed_at"] = datetime.now().isoformat(timespec="seconds")
    if note:
        payload["note"] = str(note)
    _write_deploy(payload)


def reset_deploy_node_count() -> int:
    """把台数**显式**复位成默认值（``重置并启动`` 用），返回复位后的值。

    ★ 与「删掉配置文件」不同：删掉只是**恰好**会回落到默认值，
    而且如果哪天默认值改了、或者读文件失败，结果就不可预期。
    这里是明确的意图 —— 「重置就该回到 4 台」。

    ★ 同时把"上次从几台改到几台"的记录一起清掉：重置之后那个提示已经不成立了，
    留着会让界面显示一句关于上一轮的话。
    """
    write_deploy_node_count(DEFAULT_NODE_COUNT)
    return DEFAULT_NODE_COUNT


# ---------------------------------------------------------------------------
# 端口（后端 / 前端 / 每一台存储节点）
#
# ★★ 与台数**住在同一个文件**（``nodes/deploy.json``）、走同一条
#   「界面改 → 提示重启 → 重启后生效」的链路。理由与台数完全一样
#   （见 :data:`DEPLOY_FILE` 的注释）：它必须活得比任何一个进程久，
#   而改它的入口只该有一个。
#
#   ``ports``       下一次重启要用的那一套（管理员界面保存的就是它）
#   ``stop_ports``  **历史上用过**的那些端口（去重、升序）—— 停止时要扫它们
#
# ★★ 为什么非得记 ``stop_ports``（这条不做就会出事）：停进程是按端口扫的
#   （见 ``scripts/start_all.stop_everything``）。配置一改，那里读到的就是
#   **新**端口，而正在跑的进程还在**旧**端口上 —— 于是
#   「改端口 → 点立刻重启」会一个都停不掉：旧后端还占着旧端口、还开着同一个
#   ``vds.db``，而且内存里还持着一份全局向量（见 ``backend/main.py`` 的文件头，
#   "必须单进程单 worker，全局向量在内存里"）—— 新旧两个后端同时写一个 SQLite
#   是最容易把库弄脏的场景。
#
# ★★ 为什么是个**累加的集合**、而不是"上一次跑的那一套"：端口可以被改好几次
#   （改 A → 重启 → 改 B → 还没重启就点了重置），任何一次改动都可能留下残留，
#   只记"上一次"就会漏掉更早的那一套。集合累加天然不会漏，而且停止时多扫
#   一个端口只是多一次探测 —— 真正会不会被杀，还要过一遍"这是不是本系统的
#   进程"（见 ``scripts/start_all.ours_like``），不会误杀别人的程序。
# ---------------------------------------------------------------------------

#: 三个默认端口。**这里是唯一写死的一份** —— 其余地方一律走部署配置
#: （``scripts/start_all.py`` 起进程、``frontend/vite.config.js`` 的默认值
#: 都只是"没有配置时"的兜底）。
DEFAULT_BACKEND_PORT = 8000
DEFAULT_FRONTEND_PORT = 5173
DEFAULT_NODE_BASE_PORT = 9101

#: 允许配置的端口范围。
#:
#: * 下界 1024：1024 以下是系统保留段，那一段上通常已经有别人的服务，
#:   配进去只会互相抢；而且演示环境里没有任何理由去占它。
#: * 上界就是 TCP 的端口上限。
MIN_PORT = 1024
MAX_PORT = 65535


def _as_port(raw: object, default: int | None) -> int | None:
    """把读到的值规整成一个合法端口；不合法就回 ``default``。

    **绝不抛异常**：这个文件是给人手改的，写坏一个数字不该让整条保存路径
    或整个后端起不来（与 :func:`clamp_node_count` 一个口径）。
    """
    try:
        n = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if not (MIN_PORT <= n <= MAX_PORT):
        return default
    return n


@dataclass(frozen=True)
class DeployPorts:
    """一套端口。

    ``nodes`` 按**节点 id**（``node-1``）记，不是按顺序号 —— 台数一变，
    端口记录跟着 id 一起增删，顺序上不会错位。
    """

    backend: int
    frontend: int
    nodes: dict[str, int]

    def all_ports(self) -> list[int]:
        """这一套里出现的所有端口（去重、升序）—— 探活与停止按它走。"""
        return sorted({int(self.backend), int(self.frontend), *self.nodes.values()})

    def to_dict(self) -> dict[str, object]:
        return {
            "backend": int(self.backend),
            "frontend": int(self.frontend),
            "nodes": {str(k): int(v) for k, v in self.nodes.items()},
        }

    @classmethod
    def from_raw(cls, raw: object) -> "DeployPorts | None":
        """从 JSON 里那一段读出来。不是个对象 = ``None``（**不抛异常**）。"""
        if not isinstance(raw, dict):
            return None
        nodes_raw = raw.get("nodes")
        nodes: dict[str, int] = {}
        if isinstance(nodes_raw, dict):
            for nid, val in nodes_raw.items():
                got = _as_port(val, None)
                name = str(nid).strip()
                if got is not None and name:
                    nodes[name] = got
        return cls(
            backend=_as_port(raw.get("backend"), DEFAULT_BACKEND_PORT) or DEFAULT_BACKEND_PORT,
            frontend=_as_port(raw.get("frontend"), DEFAULT_FRONTEND_PORT)
            or DEFAULT_FRONTEND_PORT,
            nodes=nodes,
        )


def read_deploy_ports() -> DeployPorts:
    """读**存下来的**那一套端口。缺的、坏的一律回默认值（不抛异常）。

    ★ 它**不负责补全**节点端口：配置里只写了 ``node-1`` 时，其余节点该用哪个
      端口由 :func:`effective_ports` 算 —— 那个函数才知道这一轮有哪几台。
    """
    got = DeployPorts.from_raw(read_deploy_info().get("ports"))
    if got is None:
        return DeployPorts(DEFAULT_BACKEND_PORT, DEFAULT_FRONTEND_PORT, {})
    return got


def read_deploy_stop_ports() -> set[int]:
    """读**历史上用过**的端口（停止时要扫一遍它们）。

    这是个**累加的集合**（见本段开头那段注释）：只记"上一次那一套"会在
    "改过好几次端口"时漏掉更早的那一批，而漏掉的后果是留下一批占着端口的
    孤儿进程（还有锁着 ``vds.db`` 的旧后端）。
    """
    raw = read_deploy_info().get("stop_ports")
    out: set[int] = set()
    if isinstance(raw, list):
        for item in raw:
            got = _as_port(item, None)
            if got is not None:
                out.add(got)
    return out


def effective_node_ids(count: int | None = None) -> tuple[str, ...]:
    """按台数推出节点名单（``node-1..node-N``）。

    与 :func:`_node_ids_from_env` 那条链是**同一个约定**：id 由台数决定，
    顺序就是集群顺序（缩容保留前 N 台、分片轮转都按它）。
    """
    n = read_deploy_node_count() if count is None else int(count)
    return tuple(f"node-{i}" for i in range(1, n + 1))


def node_ports_for(
    node_ids: Sequence[str],
    saved: dict[str, int] | None = None,
    *,
    reserved: Iterable[int] = (),
) -> dict[str, int]:
    """给这批节点算出**一套完整、互不重复**的端口。

    规则刻意简单、可预测（使用者要能在点保存**之前**就知道会得到什么）：

    1. ``saved`` 里有、且合法、且不与已占的重复 → 用它（按 ``node_ids`` 顺序认）；
    2. 其余按 ``node_ids`` 的顺序，从 :data:`DEFAULT_NODE_BASE_PORT` 起
       **找第一个没被占用的端口**补上。

    :param reserved: "这一套之外还要用的端口"（后端与前端），先占上再说 ——
        否则一个手改过的配置（把 node-1 填成 8000）会让节点与后端抢同一个端口，
        而表现是"其中一样起不来"，很难一眼看出是配置的问题。

    ★ 为什么新机器**不**简单取 ``9101 + i``：自定义端口之后那个算式会撞车
      —— 比如 node-1 被改成了 9105，那么 node-5 的默认值 9105 正好撞上。
      按顺序找空位是确定性的，而且重复调用结果一致（幂等）。
    """
    saved = saved or {}
    used: set[int] = {int(p) for p in reserved}
    out: dict[str, int] = {}
    for nid in node_ids:
        got = _as_port(saved.get(str(nid)), None)
        if got is None or got in used:
            continue
        out[str(nid)] = got
        used.add(got)
    cursor = DEFAULT_NODE_BASE_PORT
    for nid in node_ids:
        name = str(nid)
        if name in out:
            continue
        while cursor in used and cursor < MAX_PORT:
            cursor += 1
        # 65535 个端口里最多 32 台 + 2 个角色，走不到"排满"那一步。
        out[name] = cursor
        used.add(cursor)
        cursor += 1
    return out


def effective_ports(
    node_ids: Sequence[str] | None = None,
) -> tuple[DeployPorts, list[str]]:
    """**真正会用的**那一套端口，外加一份"替你改了哪几个"的说明。

    它比 :func:`read_deploy_ports` 多做了两件事，两件都是为了"人手改坏过的配置
    也能起得来"：

    * 缺的节点端口按 :func:`node_ports_for` 补上；
    * 撞车的（两个节点填了同一个、或节点端口占了后端/前端的位置）重新分配。

    ★ 改了就必须**说出来**（返回值第二项）—— 静默改配置是更坏的行为：
      使用者以为端口是自己填的那个，实际跑在另一个上，排查会绕远路。
    """
    ids = tuple(node_ids) if node_ids else effective_node_ids()
    stored = read_deploy_ports()
    nodes = node_ports_for(
        ids, stored.nodes, reserved=(stored.backend, stored.frontend)
    )
    notes: list[str] = []
    for nid in ids:
        raw = stored.nodes.get(str(nid))
        if raw is not None and nodes[str(nid)] != raw:
            notes.append(
                f"{nid} 的端口 {raw} 和别的端口重复了，改用 {nodes[str(nid)]}"
            )
    return DeployPorts(stored.backend, stored.frontend, nodes), notes


def check_port_conflicts(
    backend: int, frontend: int, nodes: dict[str, int]
) -> list[str]:
    """同一次配置里**自己撞自己**的端口；返回一句句中文，空列表 = 没问题。

    ★ 这一类**必须拦**（不像"被别的程序占用"只提示一下就放行）：
      两样东西填同一个端口，重启时后起的那个**一定**起不来 —— 那不是风险，
      是确定失败。所以 ``PUT /api/admin/ports`` 对它返回 409，而不是一条警告。
    """
    problems: list[str] = []
    seen: dict[int, str] = {}
    for label, port in (("后端", int(backend)), ("前端", int(frontend))):
        if port in seen:
            problems.append(f"{label}与「{seen[port]}」填了同一个端口 {port}")
        else:
            seen[port] = label
    for nid, port in nodes.items():
        if port in seen:
            problems.append(f"{nid} 的 {port} 与「{seen[port]}」重复")
        else:
            seen[port] = nid
    return problems


def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """这个端口上现在有没有人在听。

    ★ 判据是**真的去 bind 一下**，而且**绝不能带 ``SO_REUSEADDR``**：
      Windows 上带了它就能"抢占"别人正在监听的端口，于是探测结果假阴性
      （明明占着却报"空闲"）—— 那比不探测更坏，使用者会照着它改成一个
      起不来的端口。
    ★ 只探 ``127.0.0.1``：本系统三个角色都绑回环
      （见 ``scripts/start_all.py`` 与 ``frontend/vite.config.js`` 的 ``host``）。
      别的程序若只在别的网卡上听同一个端口号，与我们不冲突，报"空闲"是对的。
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, int(port)))
    except OSError:
        return True
    finally:
        s.close()
    return False


def write_deploy_ports(
    backend: int,
    frontend: int,
    nodes: dict[str, int],
    *,
    also_stop: Iterable[int] | None = None,
) -> None:
    """把这一套端口写进部署配置。**下次重启生效**（进程已经绑上旧端口了）。

    :param also_stop: 本次改动**之前**真正在跑的（以及更早就记下的）那些端口 ——
        它们会被并进 ``stop_ports``，供停止流程去扫。传 ``None`` = 不动那份记录。

    ★ 越界这里**抛异常**（不像读路径那样回默认值）：写进去一个非法值会让
      下次启动静默退回默认端口，那才是必须拦的（与 ``write_deploy_node_count``
      同一个理由）。
    """
    ports = DeployPorts(
        backend=int(backend),
        frontend=int(frontend),
        nodes={str(k): int(v) for k, v in nodes.items()},
    )
    for p in ports.all_ports():
        if not (MIN_PORT <= p <= MAX_PORT):
            raise ValueError(f"端口必须在 {MIN_PORT}..{MAX_PORT} 之间，收到 {p}")
    payload: dict[str, object] = dict(read_deploy_info())
    payload["ports"] = ports.to_dict()
    if also_stop is not None:
        seen = read_deploy_stop_ports() | {int(p) for p in also_stop} | set(
            ports.all_ports()
        )
        payload["stop_ports"] = sorted(p for p in seen if MIN_PORT <= p <= MAX_PORT)
        payload["ports_changed_at"] = datetime.now().isoformat(timespec="seconds")
    _write_deploy(payload)


def reset_deploy_ports(node_ids: Sequence[str] | None = None) -> DeployPorts:
    """把端口**显式**复位成默认值（``重置并启动`` 用），返回复位后的那一套。

    ★★ 调用时机有讲究：必须在**停掉旧的那一套之后**再调用
      （见 ``scripts/start_all.main`` 里 ``--reset`` 那一段）。反过来的话，
      停止流程读到的已经是默认端口，而正在跑的进程还在自定义端口上 ——
      一个都停不掉，全变成孤儿进程。

    ★ 所以这里把"复位前的端口"并进 ``stop_ports``：复位之后仍然找得到它们，
      下一次停止/重启同样能收干净。
    """
    ids = tuple(node_ids) if node_ids else effective_node_ids()
    old = read_deploy_ports()
    also_stop = read_deploy_stop_ports() | set(old.all_ports())
    fresh_nodes = node_ports_for(
        ids, reserved=(DEFAULT_BACKEND_PORT, DEFAULT_FRONTEND_PORT)
    )
    write_deploy_ports(
        DEFAULT_BACKEND_PORT, DEFAULT_FRONTEND_PORT, fresh_nodes, also_stop=also_stop
    )
    return DeployPorts(DEFAULT_BACKEND_PORT, DEFAULT_FRONTEND_PORT, fresh_nodes)


def _node_ids_from_env() -> tuple[str, ...]:
    """节点名单。``VDS_NODE_IDS`` 优先（脚本/测试显式指定），否则读部署配置。"""
    raw = os.environ.get("VDS_NODE_IDS", "").strip()
    if raw:
        return tuple(x.strip() for x in raw.split(",") if x.strip())
    return tuple(f"node-{i}" for i in range(1, read_deploy_node_count() + 1))


def node_urls_for(count: int, base_port: int = 9101) -> dict[str, str]:
    """``node-1..node-N`` → ``http://127.0.0.1:9101..``。给脚本用，避免各写一份。"""
    return {
        f"node-{i}": f"http://127.0.0.1:{base_port + i - 1}" for i in range(1, int(count) + 1)
    }


def parse_node_urls(raw: str) -> dict[str, str]:
    """解析 ``node-1=http://127.0.0.1:9101,node-2=http://127.0.0.1:9200``。

    ★ 做成**公共函数**是因为有两个调用方：:func:`_node_urls_from_env`（后端读
      ``VDS_NODE_URLS``）与 ``scripts/run_nodes.py``（起节点时要按同一份地址
      起，"每台一个端口"就是靠它表达）。格式只有一份实现，就不会两边分叉。
    """
    out: dict[str, str] = {}
    for chunk in (raw or "").split(","):
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
    return out


def _node_urls_from_env() -> dict[str, str] | None:
    """从 ``VDS_NODE_URLS`` 解析 ``node-1=http://127.0.0.1:9101,node-2=...``。

    没设这个环境变量 = 单进程模式（节点是进程内对象），
    设了 = 真分布式模式（节点是独立进程）。两种模式共用同一份
    :class:`~core.node_state.NodeState` 逻辑，所以切换不影响任何代数结果。

    端口由部署配置决定（见 :func:`effective_ports`），启动器按它拼这个变量 ——
    这里只管解析，**不关心**每台用的是连号还是各自一个端口。
    """
    raw = os.environ.get("VDS_NODE_URLS", "").strip()
    if not raw:
        return None
    return parse_node_urls(raw) or None


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
        有测试钉住这一点。端口由 ``nodes/deploy.json`` 决定（管理员界面可改），
        启动器把它拼成这个变量 —— 所以后端**不认识任何具体端口号**。
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

    #: SQLite 文件路径。★ 支持 ``VDS_DB_PATH`` 覆盖 —— 演示 / 冒烟 / 自检
    #: 可以用一个临时库跑完整流程，**不碰**正式那份 ``vds.db``。
    #: （以前只能靠“换个目录跑”，很容易误伤正式库。）
    db_path: Path = field(
        default_factory=lambda: Path(
            os.environ.get("VDS_DB_PATH") or (BASE_DIR / "vds.db")
        )
    )
    secret_key: str = field(
        default_factory=lambda: os.environ.get("VDS_SECRET_KEY") or secrets.token_urlsafe(32)
    )
    #: 签名密钥是不是**临时生成**的（没给 ``VDS_SECRET_KEY``）。
    #:
    #: ★ 这个标志存在的意义（安全审计 P3）：临时密钥**每次启动都换**，
    #:   于是所有已签发的令牌立刻失效 —— 单进程 demo 里那是“设计如此”，
    #:   但**必须能被看见**：以前没有任何地方提示它，
    #:   用户只会看到“刚登录了怎么又要登录”。
    #:   现在启动时会打一条 warning（见 ``backend/main.py``）。
    secret_key_is_temporary: bool = field(
        default_factory=lambda: not bool(os.environ.get("VDS_SECRET_KEY"))
    )
    modulus_bits: int = 1024
    l: int = 256
    #: 全局位置上限。1 KB 一块 ⇒ **文件上限 = n_max 块 ≈ 8 MB**。
    #:
    #: ★ 上传里那一步"承诺"的耗时 ∝ 块数（本机实测约 5.8 ms/块，
    #:   与块大小无关）⇒ 1 MB 文件约 6 s、8 MB 文件约 48 s。
    #:   要把上限再往上顶，先看这条耗时再决定。
    n_max: int = 8192
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
