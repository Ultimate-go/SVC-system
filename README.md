# 可验证分布式存储与可控共享系统

基于**增量聚合向量承诺**（Vector Commitment）的可验证分布式存储系统：文件分块加密后分发到多台存储节点，
协调者维护常数大小的承诺与证据；浏览器端可独立复算并验证，**服务端无法伪造结论**。

> 方案来源：ASIACRYPT 2020 §5.2 / 会议版 §8 = 全文版 §7，采用「一文件一向量 + 跨文件合并」的构造。
> 全国密码技术竞赛参赛项目，功能已完整闭环。

---

## 一、核心能力

| 能力 | 说明 |
|---|---|
| 可验证存储 | 摘要与证据大小均为**常数**（上界 256 字节），与文件大小、块数无关 |
| 跨文件聚合 | 一份证据可同时覆盖多个文件、多个用户；合并本身**不向节点取数据** |
| 一步分解 | 从一份证据拆出任意子集，跳着拆、不必逐层回退 |
| 加密存储 | 每块独立块密钥，SM4 加密；块密钥用用户 SM2 公钥做 ECIES 封装 |
| 私钥自持 | 私钥由口令（PBKDF2-SM3）封装后存库，服务端不持有明文私钥 |
| 存储证明 | 支持 Proof of Retrievability，验证节点确实持有数据 |
| 可审计 | 上传 / 改块 / 删除 / 解密 / 管理动作全部入审计流水 |
| 故障演练 | 可模拟节点掉线、副本不足、永久损毁，并给出"该坏/不该坏"的真实反应 |
| 浏览器自验 | 加密、解密、验证全部在浏览器完成，前端实现与 Python 权威输出逐字节对齐 |

**验证不受限、解密受限**：任何登录用户可验证任何文件的块；解密只对文件所有者开放（管理员亦然）。

---

## 二、技术栈

| 层 | 技术 |
|---|---|
| 算法内核 `svc/` | 纯 Python 标准库（零第三方依赖）：向量承诺、隐藏阶群、素数映射 |
| 应用层 `vds/` | 纯 Python 标准库：编码、摘要、存储/客户端节点算法、更新、PoR |
| 系统层 `core/` | Python：会话与 CRS、承诺向量存储、节点状态机、传输层 |
| 后端 `backend/` | FastAPI + SQLAlchemy 2 + SQLite（WAL）+ PyJWT + bcrypt |
| 节点 `node_service/` | 独立进程的 HTTP 服务，每台节点一套独立 SQLite |
| 前端 `frontend/` | Vue 3 + Vite + Pinia + Element Plus；自实现 SM2 / SM3 / SM4 / PBKDF2 |
| 测试 | Node 自带 `node --test`（162 条）；验收脚本 8 份 + 静态自检 4 份 |

---

## 三、快速开始

环境：Windows 10/11、Python ≥ 3.10、Node.js（满足 Vite 8）。

```powershell
python -m pip install -r requirements.txt
npm --prefix frontend install
```

启动（推荐直接双击）：

```
一键启动.cmd          # 起节点 + 后端 + 前端，并打开浏览器
停止.cmd              # 停掉全部进程
重置并启动.cmd         # 清库 + 节点数据后重新启动
```

命令行等价形式：

```powershell
python -X utf8 scripts/start_all.py --demo
```

| 入口 | 地址 |
|---|---|
| 前端 | http://127.0.0.1:5173 |
| 后端 API 文档 | http://127.0.0.1:8000/docs |

端口与节点台数写在 `nodes/deploy.json`，也可在「管理员工作台 → 存储节点」页修改。

---

## 四、演示账号

口令统一 `vds12345`：

| 账号 | 角色 | 用途 |
|---|---|---|
| `admin` | 管理员 | 用户 / 存储节点 / 审计 / 初始化与系统自检 |
| `zhangsan` | 普通用户 | 上传自有文件；验证全部、解密自持 |
| `wangwu` | 普通用户 | 演示"验证不受限、解密受限" |

---

## 五、目录结构

```
new_one/
├─ backend/          FastAPI 服务（路由、模型、管理事务）
├─ core/             系统层（会话、承诺向量、节点状态、传输）
├─ svc/              算法内核（向量承诺、群、素数映射；零依赖）
├─ vds/              VDS 应用层（编码、摘要、节点算法、更新、PoR）
├─ node_service/     存储节点独立进程
├─ frontend/
│  ├─ src/           页面、状态、纯 JS 密码学实现
│  └─ tests/         16 个测试文件 + 对齐向量
├─ scripts/          启动、验收、基准、自检
├─ docs/             架构说明、复现指南、开发与验收记录
├─ demo.py           单进程端到端演示
├─ nodes/            部署配置与节点数据（运行时生成）
└─ 一键启动.cmd / 停止.cmd / 重置并启动.cmd
```

---

## 六、关键设计

1. **一文件一向量**：每份文件在自己的向量上承诺，改动一份文件**完全不触碰**别人那份的摘要。
2. **素数按块身份派生**：块的身份是 `(owner, file_key, block_idx)`，与"全系统第几个块"无关。
   因此没有"全系统位置预算"上限，删除后重传同一块拿到的仍是同一个素数（无空洞）。
3. **`offset` 只作存储槽位**：它决定密文存在哪，**不参与任何验证**。
4. **证据常数大小**：覆盖 1 块与覆盖上万块，证据都是两个群元素。
5. **验证在本地**：本地 δ 锚（TOFU）一旦建立，服务端无法替换验证目标。
6. **两种坐标长期共存**：新文件按块身份派生素数，老文件查素数表；合并时各按各的坐标。

完整分层、模块职责、数据模型与不变式见 [docs/架构说明.md](docs/架构说明.md)。

---

## 七、验证

### 7.1 前端单元测试

```powershell
npm --prefix frontend test
```

当前 **16 个测试文件、162 条用例**，覆盖：密码学与 Python 逐字节对齐、异步 KDF 等价性、
本地 δ 锚、422 文案还原、输入校验上限、切块阶梯同表、体积估算上界、时间显示、文案安全承诺等。

### 7.2 八份可离线验收脚本（不启服务）

```powershell
python -X utf8 scripts/verify_identity_primegen.py
python -X utf8 scripts/verify_session_identity.py
python -X utf8 scripts/verify_store_identity.py
python -X utf8 scripts/verify_backend_identity.py
python -X utf8 scripts/verify_node_wire.py
python -X utf8 scripts/verify_fault_drill.py
python -X utf8 scripts/verify_incremental_agg.py
python -X utf8 scripts/verify_segment_ladder.py
```

### 7.3 四项静态自检

```powershell
python -X utf8 scripts/check_stage_names.py        # 前端步骤 key ↔ 后端埋点名
python -X utf8 scripts/check_error_codes.py        # 后端错误码 ↔ 前端 errors.js
python -X utf8 scripts/check_node_report_cache.py  # 节点现状缓存的失效时机
python -X utf8 scripts/check_js_wrap.py            # 「JS 封 → Python 解」互通
```

### 7.4 接口层端到端（需服务在跑）

```powershell
python -X utf8 scripts/_e2e_api.py
```

逐项判定标准与失败处理见 [docs/开发与验收记录.md](docs/开发与验收记录.md)。

---

## 八、界面文案约定

界面上的句子由**前端拥有**（`frontend/src/data/copy.js`），后端只回数据（计数、名单、端口、毫秒数）。
理由：改一个字的措辞不应要求重启后端。约定如下：

1. 文案是**纯函数**：输入是响应字段，不读组件状态，因此可被 `frontend/tests/copy.test.js` 直接钉住。
2. 文案里**不出现 Markdown 记号**（`**`、反引号）——它们会原样显示给用户。
3. 涉及安全承诺的句子（如"本演练不删数据"）必须保留原意，改动需同步更新测试。
4. 界面给人看的块号**从 1 起算**；代码内部的 `block_idx` 从 0 起算，两者不得混用。

---

## 九、文档

| 文档 | 内容 |
|---|---|
| [docs/架构说明.md](docs/架构说明.md) | 分层、模块职责、数据模型、数据流、设计决定、不变式 |
| [docs/复现指南.md](docs/复现指南.md) | 环境、安装、启动参数、端口、账号、验收脚本用法、常见问题 |
| [docs/开发与验收记录.md](docs/开发与验收记录.md) | 验收项、判定标准、失败处理、前端测试清单 |

---

## 十、已知限制

- **演示级参数**：模数 1024 位、分量 256 位，仅供演示；论文配置为 16λ = 2048 位。
- **跨文件合并有代价**：合并集上重开证据，代价为 $O(\sum_f |f|)$ 次大指数模幂，与要验几块无关。
- **重置 CRS 会使浏览器本地 δ 锚失效**：界面会提示并允许重新见证，这不是数据损坏。
- **单机演示形态**：节点进程与协调者同机运行；跨机部署需要自行调整 `deploy.json` 与网络配置。

---

## 十一、许可

见 [LICENSE](LICENSE)。
