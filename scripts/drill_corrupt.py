r"""服务端损坏演练：真去把**某一台节点磁盘上的一段密文**改坏一个字节。

它与证据池里那个「故障演练」开关**分工不同**
------------------------------------------------
* 证据池的开关改的是**这次要发出去的那一份证据**，证的是**客户端**这一侧的损坏
  （浏览器缓存坏了、请求体在路上被人改了）—— 服务器上的数据一个字节都没动；
* 「服务器硬盘上的数据真坏了」是另一回事，只能真去动盘上的密文。那就是这支脚本。

它演示的是这两层判据里**第 1 层**的价值：
**块哈希层**（``SM3(密文) == 分量``）会当场抓住它，而**承诺层照样通过** ——
因为承诺的对象是摘要，你只动了密文、没动分量。这正是 README 里
"有了向量承诺就不用做块哈希是错的、两层必须都在"那句话的现场版。

★★ 关键约束：改了库**不等于**立刻生效
--------------------------------------
节点是**启动时把密文读进内存**、之后一直从内存答的（见
``core/node_state.py`` 的 ``NodeState._blobs``）。所以直接改 ``node.db``
对**正在运行**的那个节点进程**没有任何影响** —— 必须让它**重启**才看得见。

脚本会检查这一点并明确告诉你，还会把该跑的重起命令原样打印出来
（令牌从 ``nodes/token.txt`` 读，不需要你抄）。

★ 可逆
------
同一个下标的**副本持有的是逐字节完全相同**的密文：``upload`` / ``modify``
把同一份 ``ct`` 发给每一个 holder，所以另一台上那份就是现成的备份。
``--repair`` 就是把它拷回来，**不需要重传文件、也不需要清库**。

跑法（在工作区根目录）::

    python scripts/drill_corrupt.py --list
    python scripts/drill_corrupt.py --corrupt --node node-1 --index 3
    python scripts/drill_corrupt.py --repair  --node node-1 --index 3

``--corrupt`` / ``--repair`` 做完之后，按它打印的那行重起节点组，
然后回「完整性验证」页验那几个下标。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from node_service.persist import NodeDB  # noqa: E402

NODES_ROOT = ROOT / "nodes"
TOKEN_FILE = NODES_ROOT / "token.txt"
#: 协调者库。**只读**打开 —— 这里只想知道"谁是主副本"。
COORD_DB = ROOT / "vds.db"

#: 改坏的方式：把**第一个字节**的最低位翻一下。
#: 为什么挑第一个字节：密文是 SM4-CTR 的输出，翻任意一个字节都会让
#: ``SM3(ct)`` 变、而分量（摘要）一个字都不动 —— 这正是要抓的那种损坏。
#: 挑第一个只是为了让"改哪儿"有个确定的说法，便于复现。
FLIP = 0x01


def _db_path(node_id: str) -> Path:
    return NODES_ROOT / node_id / "node.db"


def _load(node_id: str) -> dict[int, bytes]:
    path = _db_path(node_id)
    if not path.exists():
        raise SystemExit(
            f"找不到 {path} —— 节点名单可能不是 {node_id}，"
            f"先用 --list 看看有哪几台。"
        )
    db = NodeDB(path)
    try:
        return db.load_blobs()
    finally:
        db.close()


def _all_nodes() -> list[str]:
    if not NODES_ROOT.exists():
        return []
    out = []
    for d in sorted(NODES_ROOT.iterdir()):
        if d.is_dir() and (d / "node.db").exists():
            out.append(d.name)
    return out


def _coordinator_holders() -> dict[int, tuple[str, list[str]]]:
    """``{全局下标: (主副本, [全部持有者，主副本在前])}``（读**协调者库**）。

    为什么一个改节点库的脚本要去读协调者库：因为**验证是优先问主副本的**
    （``core/store.py`` 的读取路径：主副本取不到才改问从副本）。
    所以改坏一份**从副本**时，整份验证照样会通过 —— 那不是脚本坏了，
    而是"你改的那一份本来就不是被问到的那一份"。

    读一次就能把这件事**提前**说清，而不是让人白跑一轮再自己猜。
    读不到（库不存在/表结构变了）就返回空字典，脚本照样能用，
    只是不再提醒主副本这回事。
    """
    if not COORD_DB.exists():
        return {}
    try:
        con = sqlite3.connect(f"file:{COORD_DB}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT global_index, holder, replicas FROM blocks"
            ).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return {}
    out: dict[int, tuple[str, list[str]]] = {}
    for gidx, holder, reps in rows:
        try:
            who = json.loads(reps or "[]") or [holder]
        except (json.JSONDecodeError, TypeError):
            who = [holder]
        out[int(gidx)] = (str(holder), [str(x) for x in who])
    return out


def _holders() -> dict[int, list[str]]:
    """``{全局下标: [持有它的节点, ...]}`` —— 用来判断"这份坏了还能不能救"。"""
    out: dict[int, list[str]] = {}
    for nid in _all_nodes():
        for i in _load(nid):
            out.setdefault(i, []).append(nid)
    return out


def cmd_list() -> int:
    nodes = _all_nodes()
    if not nodes:
        print(f"{NODES_ROOT} 下没有任何节点库 —— 先启动一次。")
        return 1
    holds = _holders()
    coord = _coordinator_holders()
    print(f"节点库位置：{NODES_ROOT}")
    print()
    for nid in nodes:
        blobs = _load(nid)
        idx = sorted(blobs)
        recoverable = [i for i in idx if len(holds.get(i, [])) > 1]
        primary = [i for i in idx if coord.get(i, ("",))[0] == nid]
        print(f"  {nid:8s} 共 {len(idx):3d} 段密文（其中主副本 {len(primary)} 个）")
        print(f"           下标：{_span(idx)}")
        print(f"           有副本、改坏了能救回来的：{len(recoverable)} 个")
    only = sorted(i for i, who in holds.items() if len(who) == 1)
    if only:
        print()
        print(f"  ⚠ 只有**一份**、没有任何副本的下标：{_span(only)}")
        print("    这几个改坏了就只能重传那份文件（或 --reset）。")
    if coord:
        print()
        print("  主副本一览（验证优先问它）:")
        for gidx in sorted(coord)[:12]:
            primary, who = coord[gidx]
            print(f"    下标 {gidx:3d}  主副本 {primary:8s}  全部持有者：{'、'.join(who)}")
        if len(coord) > 12:
            print(f"    …… 共 {len(coord)} 个下标")
    return 0


def _span(idx: list[int]) -> str:
    """``[0,1,2,5]`` → ``0-2, 5``。与界面上的写法保持一致。"""
    if not idx:
        return "（空）"
    parts: list[str] = []
    lo = prev = idx[0]
    for x in idx[1:]:
        if x == prev + 1:
            prev = x
            continue
        parts.append(f"{lo}" if lo == prev else f"{lo}-{prev}")
        lo = prev = x
    parts.append(f"{lo}" if lo == prev else f"{lo}-{prev}")
    return ", ".join(parts)


def _restart_hint() -> str:
    token = ""
    if TOKEN_FILE.exists():
        token = TOKEN_FILE.read_text(encoding="utf-8").strip()
    nodes = ",".join(_all_nodes()) or "node-1,node-2,node-3,node-4"
    token_arg = token or "<nodes/token.txt 里那串>"
    return (
        "  1) 停掉节点组（后端与前端**不用动**）：\n"
        "       python scripts\\start_all.py --stop\n"
        "       （然后照常启动：双击 一键启动.cmd）\n"
        "     —— 或者只重起节点组：\n"
        f"       python scripts/run_nodes.py --nodes {nodes} --token {token_arg}"
    )


def cmd_corrupt(node_id: str, index: int) -> int:
    blobs = _load(node_id)
    if index not in blobs:
        raise SystemExit(
            f"{node_id} 没有第 {index} 段的密文 —— 先用 --list 看它持有哪些下标。"
        )
    holders = _holders().get(index, [])
    others = [n for n in holders if n != node_id]
    before = bytes(blobs[index])
    after = bytearray(before)
    after[0] ^= FLIP
    after = bytes(after)

    path = _db_path(node_id)
    db = NodeDB(path)
    try:
        db.save_blobs({index: after})
    finally:
        db.close()

    print(f"已把 {node_id} 上第 {index} 段的密文改坏一个字节（只翻了第 1 个字节的最低位）。")
    print(f"  文件：{path}")
    print(f"  长度没变：{len(before)} → {len(after)} 字节（所以「声称 == 实存」那项照样通过 ——")
    print("            改的是**内容**，不是账目）")
    print(f"  节点此刻持有的下标没变，所以**账目对得上、承诺层也会通过**。")
    print()
    if others:
        print(f"  这一份还有别的副本在：{'、'.join(others)} —— 想修回来：")
        print(f"    python scripts/drill_corrupt.py --repair --node {node_id} --index {index}")
    else:
        print(f"  ⚠ 第 {index} 段**没有别的副本** —— 改坏了只能重传那份文件（或 --reset）。")

    # ★ 主副本这一条必须说：验证优先问主副本，改从副本是看不见的。
    print()
    coord = _coordinator_holders().get(index)
    if coord is None:
        print("  （读不到协调者库 vds.db，无法判断这份是不是主副本 ——")
        print("    如果验证照常通过，很可能就是这个原因。）")
    else:
        primary, who = coord
        if node_id == primary:
            print(f"  ✓ {node_id} 正是第 {index} 段的**主副本** —— 验证会问到它。")
        else:
            print(f"  ⚠ 但**主副本是 {primary}**（持有者：{'、'.join(who)}）。")
            print("    验证优先问主副本，只在它取不到时才改问从副本 ——")
            print(f"    所以你改的这份**很可能看不出任何异常**。想看到失败：")
            print(f"      · 改主副本那一份：--node {primary} --index {index}，或者")
            print(f"      · 先把 {primary} 停掉，让读取回落到你改的这份上。")
    print()
    print("★ 但现在**还看不到任何异常**：节点是从内存里答的。要让改动生效，")
    print("  必须重起节点组：")
    print(_restart_hint())
    print()
    print("  重起后回「完整性验证」页验这个下标（或整份文件），应当看到：")
    print("    块哈希层 = 失败，而承诺层 = 通过。")
    print("  这就是「两层必须都在」的证据：只验承诺的话，这次损坏**一个字都不会报**。")
    return 0


def cmd_repair(node_id: str, index: int) -> int:
    holders = _holders().get(index, [])
    others = [n for n in holders if n != node_id]
    if not others:
        raise SystemExit(
            f"第 {index} 段只有 {node_id} 自己持有 —— 没有任何副本可以拷回来。\n"
            "只能重传那份文件（或 --reset 清库重来）。"
        )
    good = _load(others[0])[index]
    path = _db_path(node_id)
    db = NodeDB(path)
    try:
        db.save_blobs({index: good})
    finally:
        db.close()
    print(f"已从 {others[0]} 把第 {index} 段的密文拷回 {node_id}（{len(good)} 字节）。")
    print(f"  依据：同一个下标的副本是**逐字节相同**的（upload / modify 把同一份密文发给每个 holder），")
    print("        所以副本就是现成的备份，不需要重传文件。")
    print()
    print("★ 同样要重起节点组才生效：")
    print(_restart_hint())
    print()
    print("  重起后回「完整性验证」页再验一次，两层应当都恢复通过。")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="服务端损坏演练：改坏/修回某台节点磁盘上的一段密文"
    )
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--list", action="store_true", help="看各台持有哪些下标、哪些有副本")
    g.add_argument("--corrupt", action="store_true", help="把某一段密文改坏一个字节")
    g.add_argument("--repair", action="store_true", help="从副本把某一段密文拷回来")
    ap.add_argument("--node", help="节点名，如 node-1")
    ap.add_argument("--index", type=int, help="**全局下标**（不是块序号）")
    args = ap.parse_args(argv)

    if args.list:
        return cmd_list()

    if not args.node or args.index is None:
        ap.error("--corrupt / --repair 都要同时给 --node 与 --index")

    if args.corrupt:
        return cmd_corrupt(args.node, args.index)
    return cmd_repair(args.node, args.index)


if __name__ == "__main__":
    raise SystemExit(main())
