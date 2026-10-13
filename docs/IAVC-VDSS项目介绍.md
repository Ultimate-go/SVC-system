# IAVC-VDSS 项目介绍：全貌、密码学原理与前端使用

> 本文基于对整个项目 `C:\Users\Lenovo\Desktop\CTFtest\mima\test_more\new_one` 的通读（前端为主、后端与密码学核心为辅），结合下列材料整理：
>
> - `docs/方案设计.md`（设计文档）
> - `svc/scheme.py`、`svc/mathbase.py`（向量承诺核心与数学原语）
> - 亚密 2020 论文 *Incremental Aggregation and Dynamically-Sized Commitments for Vectors*（eprint 2020/149，ASIACRYPT 2020）

---

## 一、项目概述

本项目名为 **IAVC-VDSS**（Incrementally Aggregatable Vector Commitment based Verifiable Decentralized Storage System），即"基于可增量聚合向量承诺的可验证去中心化存储系统"。它的核心目标是：在一个由多个互不信任的存储节点组成的系统中，让用户能够把文件分块存入不同节点，同时获得一份**公开可验证、可聚合并可拆分**的完整性证据（承诺 δ），而无需依赖任何中心化权威来担保数据未被篡改。

其理论基石是 Lahiri、Lombardi、Mina 与 Quitté 在亚密 2020 提出的可增量聚合向量承诺（Incrementally Aggregatable Vector Commitments, IAVC），具体实现采用了论文第 5.2 节给出的"第二种 SVC 构造（Second SVC Construction）"，并在第 8.2 节"可验证去中心化存储（VDS）"的框架之上落地。项目设计文档明确指出，它在论文"设计 B（单全局向量）"的基础上做了两处工程化偏离：

1. **一个文件对应一个向量**，每个文件拥有自己独立的 `δ = (U, C, n)`；
2. **素数由分块身份推导**，即 `e_i = H(owner‖file_key‖i)`，而非维护一张全局素数表。

这两处改动消除了论文方案里对 `n_max` 上限和"空洞"的限制，使文件长度可动态增长。

系统整体采用**单进程、单 worker 的协调者（Coordinator）架构**：协调者 `StoreManager` 负责把全局向量状态写穿（write-through）到 SQLite，并在内存中维护全局向量；之下挂接 N 个存储节点（每个节点各自持有一份 SQLite），分别持有密文分块以及节点本地的 δ。后端为 FastAPI + SQLite + JWT，前端为 Vue 3 + Vite + Element Plus + Pinia。

---

## 二、基本原理（密码学核心）

### 2.1 隐藏阶群承诺（Hidden-Order Group Commitment）

IAVC 的安全性建立在**隐藏阶群**（RSA 类群）之上。系统参数 `setup` 生成模数 `N = p·q`（论文配置 `|N|=2048`，本演示为 1024），以及生成元 `g`，其中 `φ(N)` 对任何参与方均不可知——这正是 RSA 假设的核心：已知合数 N，却无法得知其欧拉函数值，从而无法计算任意元素的 e 次根。

每个向量位置 i 绑定一个 `(l+1)` 比特素数 `e_i`（演示中 `l=256`）。令：

- `e_I = ∏_{i∈I} e_i`（子向量 I 的素数积）；
- `e_[n] = ∏_{i=1}^n e_i`（前 n 个位置的素数积）；
- `S_i = g^{e_[n]/e_i}`，进而 `S_I = g^{e_[n]/e_I} = U_n^{1/e_I}`，其中 `U_n = g^{e_[n]}`；
- 向量承诺 `C = ∏ S_i^{v_i}`；
- 证明 `Λ_I = (∏_{j∉I} S_j^{v_j})^{1/e_I}`。

由此，对子向量 I 的**开证明（opening proof）只需两个群元素** `π_I = (S_I, Λ_I)`，**与向量长度 n、子向量大小 |I| 完全无关**——这是向量承诺相对 Merkle 树最本质的优势：证明大小恒定。

验证等式为：

```
S_I^{e_I} = U_n  ∧  C = Λ_I^{e_I} · ∏_{i∈I} S_i^{v_i}
```

### 2.2 "摊开根"技巧（Spreading the Root）

注意 `Λ_I` 的定义里含有 `1/e_I` 次根，若真去计算它，就等价于攻破 RSA 假设。论文的巧妙之处在于：**这个根从未被真正计算**。项目通过 `weighted_root_product` 把 `1/e_I` "摊开"成若干整数指数，使每一项都退化为普通的模幂运算；验证阶段更用顺序 `add_back` 把复杂度从论文 `O(ℓ·|I|²)` 降到 `O(ℓ·|I|)`（`ℓ` 为素数比特长）。`mathbase.py` 中的 Baillie–PSW 素性测试（`is_bpsw_prime`）、扩展欧几里得（`egcd`/`mod_inverse`）、多指数 `multiexp` 与均衡乘积树 `product_tree` 共同支撑了这套数学。

### 2.3 聚合与拆分（Aggregation / Disaggregation）

聚合通过 **Shamir 技巧（Bézout 恒等式）** 实现：`shamir_trick` 对两个不交子向量 I、J 的互素 `e_I`、`e_J` 做线性组合，把两份证明 `π_I`、`π_J` 合并为一份覆盖 `I∪J` 的证明 `π_{I∪J}`。拆分 `disagg` 则是聚合的逆运算，依赖反复调用的 `add_back`：`S' = S^{e_i}`、`Λ' = Λ^{e_i}·S^{v_i}`，每次 `O(ℓ)` 次群运算。这使得"任意子向量的证据可独立验证、可随意拼合/拆解"成为系统的底层原语。

### 2.4 国密算法的接入（SM2 / SM3 / SM4）

项目并未用国密做数字签名，而是把国密族用于**密钥封装与数据加密**：

- **SM2**：用于以"椭圆曲线集成加密（ECIES）"把每个分块的块密钥封装给文件拥有者；同时个人中心展示的正是用户本人的 SM2 私钥（d）。
- **SM3**：向量元素 `v_i` 取为 `SM3(密文)`；同时在 KDF、以及口令拉伸 `PBKDF2-HMAC-SM3` 中充当哈希原语。
- **SM4-CTR**：对明文按段加密，每段使用独立的密钥与 IV。

后端（`cryptography` 库）与前端（手写 JS 国密实现）都各自实现了一套，并配有跨语言测试向量以保证一致。

### 2.5 两层密钥封装与"验证无限制 / 解密受限制"

`core/keywrap.py` 设计了双层封装：

- **第一层**——分块密钥用拥有者 SM2 公钥做 ECIES 封装；
- **第二层**——用户私钥本身用登录口令经 `PBKDF2-HMAC-SM3` 派生出 KEK 后，以 `SM4-CTR` 封装并附带 KDF 标签。

私钥**绝不以明文落库**；在默认的"client"登录模式下，后端只回传 `key_blob`（密文），由浏览器本地解封，从而后端即便被攻破也拿不到明文私钥。

由此引出系统的两条红线语义：

- **验证无限制（Verify-unrestricted）**：δ 公开，任何人都能验证任意位置（跨文件、跨用户）的数据完整性；
- **解密受限制（Decrypt-restricted）**：能否还原明文，由是否持有拥有者 SM2 私钥在密码学上强制执行，而非靠一张 ACL 权限表。

---

## 三、前端使用与交互

前端基于 **Vue 3 + Vite + Element Plus + Pinia**，通过 `auth.js`（登录态/令牌）、`crypto.js`（内存中持有解封后的 SM2 私钥 `_sk`，不进 localStorage）、`guard.js`（路由守卫）三块协同。路由表共 18 条，可概括为：

- **公开页**：`/login`（登录与自助注册）、`/403`、`/404`。
- **登录后通用页**：仪表盘 `/`、个人中心 `/profile`、设置 `/settings`、性能页 `/perf`。
- **文件与证据页**：`/files`（上传/列表）、`/files/:id`（详情）、`/files/update` 及 `/files/update/:id`（追加/更新分块）、`/evidence/pool`（证据池）、`/evidence/verify`（在线验证，可演示 disagg/agg）。
- **管理页（仅 admin）**：`/users`、用户新增/详情、设备 `/devices` 及增删改、审计 `/audit`。

### 3.1 登录与注册流程

登录页 `login/index.vue` 提供"登录 / 注册"双模式切换。注册为普通用户自助通道：后端 `POST /api/auth/register` 强制 `role="user"`（schema `RegisterIn` 不含 role 字段，无法越权注册管理员），校验重名后建库并调用 `mgr.ensure_user_key` 生成用户 SM2 密钥对，私钥经口令封装后回传 `key_blob`。注册成功即自动登录——浏览器用口令在本地解封私钥存入 `crypto.js` 的内存变量；之后访问 `/profile` 即可在"SM2 密钥对"面板中看到公钥完整 hex 与 Qx/Qy，并以"查看/隐藏"开关显式揭示私钥十进制值（未解锁时显示"—（未解锁…）"）。

### 3.2 文件写入与完整性承诺

用户在 `/files` 上传文件，前端按段以 **SM4-CTR** 加密，每段用独立密钥；分块身份 `H(owner‖file_key‖i)` 经 `IdentityPrimeGen` 映射为素数 `e_i`，块密文的 `SM3` 摘要作为向量元素 `v_i`。后端将该文件的向量承诺 `δ=(U, C, n)` 随密文分块一并写入节点，从而形成恒定大小、与文件长度无关的可验证证据。

### 3.3 证据验证与聚合演示

`/evidence/pool` 汇聚各文件/节点的 δ；`/evidence/verify` 允许用户对任意位置发起验证——底层走 `verify` / `verify_batch`（批验证为"刻意更慢，安全优先"的设计），并可在 UI 上演示聚合（agg）把多个子向量证明并成一份、或拆分（disagg）把一份证明解开为子证明，直观展示"证明大小恒定"这一属性。

### 3.4 管理员视角的隔离

前端在 `routes.js` 对管理页打 `requiresAdmin` 标记，`guard.js` 在路由进入前检查角色，非 admin 一律重定向到 `/403` 或 `/files`；后端 `deps.py` 的 `require_admin` 再以 403 兜底。角色仅有 `"admin"` 与 `"user"` 两档。

---

## 四、小结

IAVC-VDSS 把亚密 2020 的可增量聚合向量承诺从论文（§5.2 构造 + §8.2 VDS 框架）落地为一个工程系统：以隐藏阶群承诺保证"恒定大小、可聚合、可拆分"的完整性证明，以国密 SM2/SM3/SM4 完成密钥封装与数据加密，以两层密钥封装与"验证公开 / 解密私有"的语义界定信任边界；前端则通过 Vue 3 全家桶把这整套原语包装成登录注册、文件上链、证据验证与管理审计的完整交互闭环。两处对论文的偏离（一文件一向量、身份派生素数）让系统在保留密码学保证的同时，摆脱了全局素数表与长度上界的束缚。
