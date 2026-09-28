<div align="center">

# IAVC-VDSS

**基于增量聚合向量承诺的可验证分布式存储系统**

*Incrementally Aggregatable Vector Commitments for Verifiable Decentralized Storage*

![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![Vue](https://img.shields.io/badge/Vue-3-42b883)
![FastAPI](https://img.shields.io/badge/FastAPI-0.110%2B-009688)
![License](https://img.shields.io/badge/License-MIT-yellow)

</div>

---

## 简介

IAVC-VDSS 是论文 *Incrementally Aggregatable Vector Commitments and Applications to Verifiable Decentralized Storage*（ASIACRYPT 2020 / eprint 2020/149，§5.2 + §8.2）方案的一个完整工程实现。

核心是一条贯穿全系统的设计决定：**所有用户的所有文件都按上传顺序追加到同一条向量上**，共用一份常数大小的摘要 δ、一套公开参数。由此一次获得三个能力：常量摘要、常量证据与一证据跨多文件。
在这些密码学能力之上，系统补齐了分布式存储节点、加密存储、密钥封装、完整性验证、存储证明（PoR）、审计回放与 Web 界面，构成一个可演示、可复现的完整系统。

---

## 项目状态

- 2026年第11届全国密码技术竞赛参赛项目，功能已完整闭环。
- 全量回归 **829 项通过** ；算法核 384 项与论文 §5.2 / §8.2 逐项对应）。
- 密钥层已按导师要求从 ABE 切换为「**SM2 公钥封块密钥 + 口令封私钥**」。
- 目前仍在迭代：详见 [`docs/方案设计.md`](docs/方案设计.md) 的「已知局限」一节。

---

## 功能特性

- **可验证存储**：摘要与证据大小均为常数（上界 256 字节），与文件大小、块数无关。
- **跨文件聚合**：一份证据可同时覆盖多个文件、多个用户，天然合成一份。
- **加密存储**：SM4-CTR 分段加密（每段独立密钥与 IV）+ SM3 块哈希。
- **密钥封装**：块密钥用所有者的 SM2 公钥封装（ECIES），用户私钥用登录口令封装（PBKDF2-HMAC-SM3），全库不留明文密钥。
- **验证不受限 / 解密受限**：任何人都能验证完整性（公开可验证），只有所有者能解密（密码学强制，管理员也不行）。
- **分布式存储节点**：N 台独立进程 + 各自 SQLite；块按轮转分片，每块默认 2 份副本。
- **增量更新**：改块 / 追加 / 截断，代价只与改动量成正比，已有块一个不动。
- **存储证明（PoR）**：随机挑战，常数大小的证明确认各节点仍持有数据。
- **Web 界面**：Vue 3 + Vite + Element Plus，13 个页面（登录、总览、文件与块、证据池、验证、存储节点、性能、集群、审计、用户管理等）。
- **审计回放**：只记元数据，被拒也留痕；可按对象 / 操作者精确匹配回放。

---

## 快速开始

### Windows 一键启动（推荐）

项目根目录下双击即可：

| 脚本 | 作用 |
|---|---|
| `一键启动.cmd` | 起 4 台存储节点 + 后端 + 前端，并打开浏览器 |
| `重置并启动.cmd` | 清空演示数据、只重建账号后启动 |
| `停止.cmd` | 停掉节点（9101-9104）、后端（8000）、前端（5173） |

演示账号（口令统一 `vds12345`）：

| 用户名 | 身份 |
|---|---|
| `admin` | 管理员（可看审计、管用户，但解不开别人的文件） |
| `zhangsan` | 张三，4 份病历的所有者 |
| `nurse` / `ortho` | 护士 / 医生（能验证别人的文件，解不开） |
| `wangwu` | 王五，只有一份会议纪要 |

### 手工启动

```powershell
# 一键起全套（保留已有数据）
python scripts/start_all.py
python scripts/start_all.py --nodes 2   # 只起 2 台节点
python scripts/start_all.py --reset     # 清空重建再起
python scripts/start_all.py --stop      # 全部停掉
```

> 完整的单进程 / 跨进程两种模式、环境变量、依赖安装与接口清单，见 [`docs/复现指南.md`](docs/复现指南.md)。

---

## 项目结构

```
SVC-system/
├─ svc/                 论文 §5.2 的 SVC（算法核，零依赖）
├─ vds/                 论文 §8.2 的 VDS（算法核，零依赖）
├─ core/
│  ├─ store.py          VectorStore —— 上传/加密/承诺/分发/检索/聚合/验证/解密
│  ├─ keywrap.py        密钥封装层（取代 ABE）：公钥封块密钥 + 口令封私钥
│  ├─ sm2.py            SM2 曲线原语（手写）
│  ├─ crypto.py         SM4-CTR 分段加密 + SM3 块哈希
│  ├─ node_state.py     节点本地状态机
│  ├─ registry.py       全局块索引登记表
│  ├─ session.py        全局会话 + CRS
│  └─ transport.py      节点协议 + LocalTransport + WriteError
├─ node_service/        存储节点服务（一个进程 + 一个 SQLite）
├─ backend/             FastAPI + SQLite + JWT；验证不受限 / 解密受限
│  └─ routers/          auth / admin / files / verify / system
├─ scripts/             seed / run_nodes / start_all / smoke / bench_* / drill_corrupt
├─ frontend/            Vue 3 + Vite + Element Plus（13 个页面）
├─ demo.py              端到端演示 + 性能指标
└─ docs/                方案设计 + 复现指南
```

---

## 文档

- [`docs/方案设计.md`](docs/方案设计.md) —— 设计决定、密钥层、系统架构、需求对照、关键不变式、取舍清单与已知局限。
- [`docs/复现指南.md`](docs/复现指南.md) —— 环境要求、启动方式、演示账号、两种运行模式、接口一览与实测数字。

---

## 许可证

本项目采用 [MIT License](LICENSE)。
