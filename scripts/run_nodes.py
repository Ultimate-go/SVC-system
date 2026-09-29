r"""一次起 4 台存储节点（各自独立进程 + 独立 SQLite）。

用法::

    # 起节点（前台，Ctrl+C 一起退）
    python scripts/run_nodes.py

    # 只看看在不在
    python scripts/run_nodes.py --check

    # 换端口 / 换节点名单（**连号**，一个基址决定全部）
    python scripts/run_nodes.py --base-port 9201 --nodes node-1,node-2

    # 每台**各自一个**端口（管理员界面存下来的就是这种形式）
    python scripts/run_nodes.py --urls "node-1=http://127.0.0.1:9201,node-2=http://127.0.0.1:19301"

起好后它会打印一行 ``VDS_NODE_URLS``，把它设到**另开的一个终端**里再起后端，
后端就进入跨进程模式::

    $env:VDS_NODE_URLS = "node-1=http://127.0.0.1:9101,..."
    $env:VDS_NODE_TOKEN = "<脚本打印的那串>"
    python -m uvicorn backend.main:app --port 8099

**令牌那行不能省**：节点一律要求鉴权（请求头 ``X-Node-Token``），
否则 ``/node/reset`` 与 ``/node/retrieve`` 就只是靠"绑回环"护着，
而绑哪个地址是部署参数。没传 ``--token`` 时本脚本会现场生成一个并打印出来，
必须与后端用**同一个值**。

不设 ``VDS_NODE_URLS`` 就是单进程模式（节点是进程内对象，不用令牌）。
**两种模式的代数结果逐位相同**，所以拿哪种跑演示都可以 ——
只是跨进程模式能让"节点各自独立"这件事看得见。跨进程模式必须
**先起节点再起后端**：后端启动时要把公开参数推给节点。
"""

from __future__ import annotations

import argparse
import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.config import parse_node_urls  # noqa: E402
from node_service import NODE_TOKEN_HEADER  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_NODES = ("node-1", "node-2", "node-3", "node-4")
DEFAULT_BASE_PORT = 9101


def node_urls(nodes: tuple[str, ...], base_port: int) -> dict[str, str]:
    return {nid: f"http://127.0.0.1:{base_port + i}" for i, nid in enumerate(nodes)}


def env_line(urls: dict[str, str], token: str) -> str:
    body = ",".join(f"{k}={v}" for k, v in urls.items())
    return (
        f'$env:VDS_NODE_URLS = "{body}"\n'
        f'  $env:VDS_NODE_TOKEN = "{token}"'
    )


def health(url: str, token: str, timeout: float = 1.5) -> dict | None:
    """探活。**要带令牌** —— 不带会给 401，那不是"没起来"。"""
    req = urllib.request.Request(
        f"{url}/node/health", headers={NODE_TOKEN_HEADER: token}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            import json

            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # 401 = 活着但令牌不对。必须与"没起来"分开报 ——
        # 混成一句会把排查方向带偏（去看进程，其实进程好好的）。
        if e.code == 401:
            return {"ok": False, "_http": 401}
        return None
    except (urllib.error.URLError, OSError, ValueError):
        return None


def check(urls: dict[str, str], token: str) -> int:
    if not token:
        print("没给令牌（--token 或 VDS_NODE_TOKEN）—— 探活会全部 401。", file=sys.stderr)
        return 2
    bad = 0
    for nid, url in urls.items():
        h = health(url, token)
        if h is None:
            print(f"  {nid:8s} {url:26s} 没起来")
            bad += 1
            continue
        if h.get("_http") == 401:
            print(
                f"  {nid:8s} {url:26s} 活着但令牌不对（401）——"
                f" 检查 VDS_NODE_TOKEN 是否与起节点时一致"            )
            bad += 1
            continue
        state = "有状态" if h.get("has_state") else "空"
        crs = "已收公开参数" if h.get("has_crs") else "没有公开参数"
        blocks = h.get("db", {}).get("blobs")
        print(f"  {nid:8s} {url:26s} 活着 / {crs} / {state} / 密文 {blocks} 段")
    if bad:
        print(f"{bad} 台不正常")
        return 1
    print("全部正常。")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="起一组 VDS 存储节点")
    ap.add_argument("--nodes", default=",".join(DEFAULT_NODES))
    ap.add_argument("--base-port", type=int, default=DEFAULT_BASE_PORT)
    ap.add_argument(
        "--urls",
        default="",
        help="直接给出每台节点的地址（node-1=http://127.0.0.1:9201,...），"
        "每台端口可以不同。给了它就忽略 --nodes / --base-port。"
        "管理员界面存下来的就是这种形式（见 nodes/deploy.json 的 ports）",
    )
    ap.add_argument("--data-root", default=str(ROOT / "nodes"))
    ap.add_argument("--check", action="store_true", help="只检查在不在，不起")
    ap.add_argument("--fresh", action="store_true", help="先把各节点的数据目录删掉")
    ap.add_argument(
        "--strict",
        action="store_true",
        help="只要有一台节点退出就整组收工（默认：继续守着其余的，方便演示掉线降级）",
    )
    ap.add_argument(
        "--token",
        default=os.environ.get("VDS_NODE_TOKEN", ""),
        help="节点令牌。不传就现场生成一个并打印出来；后端必须用同一个值",
    )
    args = ap.parse_args(argv)

    nodes = tuple(x.strip() for x in args.nodes.split(",") if x.strip())
    token = args.token or secrets.token_urlsafe(24)
    if args.urls.strip():
        # ★ ``--urls`` 优先：每台节点的端口可以**各不相同**（管理员界面上就是
        #   一个个填的，见 backend/config.py 的 ``DeployPorts``）。
        #   起进程时用的端口本来就是从 URL 里取出来的（下面 ``url.rsplit``），
        #   所以这里只要把 urls 换成解析结果，其余一律不动。
        try:
            urls = parse_node_urls(args.urls)
        except ValueError as exc:
            print(f"--urls 解析失败：{exc}", file=sys.stderr)
            return 2
        if not urls:
            print("--urls 里一个节点都没有", file=sys.stderr)
            return 2
        nodes = tuple(urls)
    else:
        if not nodes:
            print("节点名单不能为空", file=sys.stderr)
            return 2
        urls = node_urls(nodes, args.base_port)

    if args.check:
        return check(urls, args.token)

    data_root = Path(args.data_root)
    if args.fresh:
        import shutil

        for nid in nodes:
            shutil.rmtree(data_root / nid, ignore_errors=True)

    procs: list[subprocess.Popen] = []
    print("起节点：")
    for nid, url in urls.items():
        d = data_root / nid
        d.mkdir(parents=True, exist_ok=True)
        cmd = [
            sys.executable,
            "-m",
            "node_service",
            "--node-id",
            nid,
            "--port",
            str(int(url.rsplit(":", 1)[1])),
            "--data-dir",
            str(d),
            "--token",
            token,
        ]
        procs.append(subprocess.Popen(cmd, cwd=str(ROOT)))
        print(f"  {nid:8s} {url:26s} 数据目录 {d}")

    # 等它们都活过来，免得后端抢先启动时连不上
    deadline = time.time() + 30
    while time.time() < deadline:
        if all(health(u, token) for u in urls.values()):
            break
        time.sleep(0.2)
    else:
        print("有节点 30 秒内没起来，把子进程收掉。", file=sys.stderr)
        for p in procs:
            p.terminate()
        return 1

    print("\n节点都活着。另开一个终端执行：")
    print("  " + env_line(urls, token))
    print("  python -m uvicorn backend.main:app --port 8099")
    print(f"\nCtrl+C 会一起停掉这 {len(procs)} 台。")
    print(
        "★ 单独 kill 掉某一台不会把这一组带走 —— 这一组继续守着其余几台，\n"
        "   正好用来当场演示「掉一台、副本兜底」：后端会把它标成掉线，\n"
        "   凡是还有副本的块照样读得到。数据目录不动，所以重启回来数据不丢。\n"
        "   想恢复“一台退出就整组收工”的旧行为，加 --strict。"
    )

    # ★★ 这里原来是一句“有节点退出了，一起收工” 就 return 1（#10）。
    #   后果是一整条链：本进程退出 → start_all.py 的 kids 里那个
    #   run_nodes 子进程 poll() 非空 → 启动器 return 1 → finally 里
    #   terminate 其余全部并按端口 kill 9101-9104 → 8000/5173 也没了，
    #   **页面所有请求变成 ERR_CONNECTION_REFUSED**。
    #   这不是“降级”，是整体下线 —— 而“kill 一台看降级”正是要演的东西。
    names = list(urls)
    reported: set[str] = set()
    try:
        while True:
            time.sleep(0.5)
            dead = [nid for nid, p in zip(names, procs) if p.poll() is not None]
            if args.strict and dead:
                print(
                    f"有节点退出了（{'、'.join(dead)}）—— --strict 下整组收工。",
                    file=sys.stderr,
                    flush=True,
                )
                return 1
            # 只报“新”倒下的那些，否则每 0.5 秒就刷一遍屏
            for nid in dead:
                if nid in reported:
                    continue
                reported.add(nid)
                p = procs[names.index(nid)]
                print(
                    f"\n[注意] {nid} 退出了（退出码 {p.returncode}）。"
                    f"其余 {len(procs) - len(dead)} 台继续跑。",
                    file=sys.stderr,
                    flush=True,
                )
                print(
                    "        后端会把它标成掉线（/api/nodes 的 unreachable）；"
                    "还有副本的块照样读得到，写操作会拒绝。\n"
                    f"        要让它回来：另开一个终端把旧的那台/那组停干净再重起（数据不丢）——\n"
                    f"          python scripts/run_nodes.py --token <本窗口打印的那串>\n"
                    "        （Ctrl+C 仍会一起停掉全部）",
                    file=sys.stderr,
                    flush=True,
                )
            # 全死了就真没什么好守的了
            if len(dead) == len(procs):
                print("所有节点都退出了，收工。", file=sys.stderr)
                return 1
    except KeyboardInterrupt:
        print("\n收工，停节点……")
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()
        for p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover
                p.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
