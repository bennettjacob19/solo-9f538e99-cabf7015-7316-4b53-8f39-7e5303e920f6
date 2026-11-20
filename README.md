# 内网 APT 源（FastAPI）

单发行版、单组件、单架构的 APT 软件源。每次发布先落盘、后切换
`Release`，启用 **Acquire-By-Hash**，并只保留最近两次成功发布所引用的
索引和安装包。拿到旧 `Release` 的客户端仍可按其中的 SHA-256 取回旧索引，
再按索引校验下载安装包；同名同版本重新构建也互不覆盖。

## 快速开始（docker compose）

```bash
docker compose up -d --build
curl -s http://127.0.0.1:8080/api/status | python3 -m json.tool
```

上传一批 `amd64` 的 `.deb`（整版发布，一次请求一批）：

```bash
curl -fsS -X POST http://127.0.0.1:8080/api/publish \
  -F files=@hello-internal_1.0.0_amd64.deb \
  -F files=@libfoo_2.1.0_amd64.deb | python3 -m json.tool
```

也可以直接用浏览器打开 <http://127.0.0.1:8080/docs> 的上传表单
（OpenAPI 文档在 `/swagger-ui`）。

## 值班场景：按 md5sums 辨认隔离区主机的构建

隔离区主机只能从 `dpkg` 的 md5sums 抄几行时，在 `/swagger-ui` 找到
`GET /api/builds/identify`，输入**包名与版本原文**（与控制字段
`Package` / `Version` 完全一致）：

```bash
curl -fsS "http://127.0.0.1:8080/api/builds/identify?package=smoke-one&version=1.0.0" \
  | python3 -m json.tool
```

返回语义：

- 按 `.deb` 的 SHA-256 去重，取最近 **6** 份成功构建；同一摘要只保留
  **最后一次成功发布号**（`publication_id` / `release_number`），按发布号
  从新到旧排列。
- `distinguishing_paths` 是区分全部不同清单所需的**最少文件路径**
  （精确最小集，并列时取字典序最小）。
- 每个构建在 `md5` 中逐路径给出 MD5；清单未列出该路径时为
  「**未列出**」。
- 清单完全相同的构建进入同一 `manifest_group` 并在 `groups` 中标注
  「清单相同，并列」——仅凭 md5sums 无法进一步区分。
- 缺少可用清单（无 `md5sums` 成员、空成员或内容不可解析）的构建
  照常发布，查询时 `note` 标为「**证据不足**」、`manifest_group` 为
  `null`。
- 失败的发布不进入查询；重启后恢复为成功的发布保留原清单。包被 GC
  回收后，清单仍留在发布 journal 中，核对结果照常生成。

响应示例（节选）：

```json
{
  "package": "smoke-one",
  "version": "1.0.0",
  "query_limit": 6,
  "distinguishing_paths": ["/etc/app.conf"],
  "not_listed_marker": "未列出",
  "groups": [
    {"group": 1, "publication_ids": [3, 2], "note": "清单相同，并列"},
    {"group": 2, "publication_ids": [1], "note": null}
  ],
  "builds": [
    {"publication_id": 4, "sha256": "b50c…", "manifest_group": null,
     "note": "证据不足", "md5": {"/etc/app.conf": "未列出"}},
    {"publication_id": 3, "sha256": "b0c9…", "manifest_group": 1,
     "note": null, "md5": {"/etc/app.conf": "4b1b2715…"}}
  ]
}
```

## 客户端配置与实测

```bash
sudo ./clients/setup-client.sh http://127.0.0.1:8080 stable main
# 等价于写入 /etc/apt/sources.list.d/internal.list：
#   deb [trusted=yes arch=amd64] http://127.0.0.1:8080/ stable main

sudo apt-get update
apt-get download hello-internal          # 下载并校验
sudo apt-get install hello-internal      # 或直接安装
```

仓库未签名，因此用 `trusted=yes`。若要签名，增加 `Release.gpg`/`InRelease`
并把 `trusted=yes` 换成 `signed-by=/usr/share/keyrings/internal.gpg`。

一键端到端验证（发布两版、`apt-get update`/`download`、旧 Release 客户端
按哈希取包、第三版触发淘汰）：

```bash
sh scripts/e2e-apt.sh http://127.0.0.1:8080
```

实测输出示例：

```
== release 1 published (index 71440bc1...)
== apt-get download smoke-one -> smoke-one_1.0.0_amd64.deb
== release 2 published (index 6fc51fd9...)
== apt-get download smoke-two -> smoke-two_2.0.0_amd64.deb
== previous release index and packages verified via by-hash
== retention keeps exactly the two most recent releases
== OK
```

## 发布语义

| 需求 | 实现 |
| --- | --- |
| 新版编制期间继续供包 | 新索引/包先写入不可变路径（`by-hash/SHA256/<hash>`、内容寻址的 pool 文件），旧 `Release` 与旧索引在此期间保持不变 |
| 切换时索引与包同时可下载 | `Release` 用原子 `rename` 作为唯一提交点；在此之前所有被引用的文件都已 fsync 落盘 |
| 旧 Release 客户端仍可取旧索引 | `Acquire-By-Hash: yes` + `dists/stable/main/binary-amd64/by-hash/SHA256/<sha256>`，`dists/stable/by-hash/SHA256/<sha256>` 亦提供 Release 本体 |
| 同名同版本重发 | pool 文件名带内容哈希：`<pkg>_<ver>_<arch>_<sha16>.deb`，两版并存 |
| 只开放最近两次成功发布 | GC 以 journal 中最新 2 条 `committed` 记录为根，删除其余 by-hash 索引与 pool 文件 |
| 留存 md5sums 供事后辨认 | 发布时从 `.deb` 控制归档解析 `md5sums` 并写入 `package_builds`；缺少清单的包照常发布（清单列为 NULL，查询标「证据不足」）。清单不随 pool 文件 GC，包回收后仍可核对 |
| 坏包 / 提交前中断两版照旧 | 上传先完整校验；任一文件非法则整批拒绝。提交中断时旧 `Release` 不变，重启后 `reconcile()` 回滚 |
| 重启后仍可取用 | 启动时对比磁盘上 `Release` 的哈希与 journal 中 `staged` 记录：一致则补记为 `committed`，否则标记 `aborted` 并清理 |

### 目录结构（数据卷内）

```
/data
├── export/                                  # 只读 HTTP 根目录
│   ├── dists/stable/Release
│   ├── dists/stable/by-hash/SHA256/<release-sha256>
│   ├── dists/stable/main/binary-amd64/Packages(.gz)
│   ├── dists/stable/main/binary-amd64/by-hash/SHA256/<index-sha256>
│   └── pool/main/<bucket>/<pkg>/<pkg>_<ver>_<arch>_<sha16>.deb
├── incoming/<token>/                        # 上传暂存，发布后删除
└── state.sqlite3                            # 发布 journal（GC 的唯一依据）
                                           # 内含 package_builds：每个构建的
                                           # SHA-256 与留存的 md5sums（可空），
                                           # 独立于 pool 文件长期保存
```

`Release` 同时列出 `Packages` 与 `Packages.gz`：APT 2.6 以未压缩名识别
索引目标，只列 `Packages.gz` 会被静默跳过（本项目最初即踩此坑）。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/publish` | multipart 字段 `files`，一次一批，返回 201 与发布记录；发布时从控制归档留存 `md5sums` |
| GET | `/api/builds/identify` | 查询参数 `package`、`version`（原文）；按 `.deb` SHA-256 去重返回最近 6 份成功构建与最少区分路径的 MD5 核对结果 |
| GET | `/api/status` | 当前配置、最新发布、发布历史 |
| GET | `/api/publications` | 发布历史（含 `aborted`） |
| GET | `/docs` | 上传表单 |
| GET | `/swagger-ui` | OpenAPI 文档（含辨认接口的参数与返回模型） |
| GET/HEAD | `/dists/...`、`/pool/...` | 仓库文件（只读） |

错误语义：包非法 / 重复的同名同版本 / 批次超限返回 `400`/`413`，且
仓库状态完全不变。

## 配置

| 环境变量 | 默认值 |
| --- | --- |
| `APT_REPO_DATA_DIR` | `./data`（容器内 `/data`） |
| `APT_REPO_DISTRIBUTION` | `stable` |
| `APT_REPO_COMPONENT` | `main` |
| `APT_REPO_ARCHITECTURE` | `amd64` |
| `APT_REPO_ORIGIN` / `APT_REPO_LABEL` | `Internal APT Repository` |
| `APT_REPO_RETENTION` | `2` |
| `APT_REPO_MAX_FILE_BYTES` | `536870912` |
| `APT_REPO_MAX_BATCH_FILES` / `_BYTES` | `500` / `4 GiB` |

## 操作约束

- **单写入进程**：发布通过进程内线程锁 + `flock` 串行化，`docker compose`
  固定 `--workers 1`。多副本部署时需改为共享存储上的分布式锁。
- **unsigned**：默认不签名；生产环境应补 `InRelease`/`Release.gpg`。
- **下载与 GC 竞态**：响应通过已打开的 fd 流式发送，即使文件随后被 GC
  删除，进行中的下载仍能读到完整内容（POSIX inode 语义）。
- 容器以只读根文件系统、非 root（uid 10001）、`no-new-privileges` 运行。

## 本地开发与测试

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest httpx
uvicorn app.main:app --reload          # 数据写入 ./data
pytest -q                              # 全部测试
```

测试覆盖：发布/回读、旧 Release 按哈希取旧索引与旧包、同名同版本重发、
两版保留与第三版淘汰、坏包整批拒绝、错误架构拒绝、提交前中断回滚、
提交后崩溃恢复、下载中被 GC、Release 格式与 by-hash 服务、路径穿越；
以及构建辨认：md5sums 解析（含真实 `dpkg-deb` 产物）、最少区分路径的
精确最小集、同清单并列、「未列出」与「证据不足」、SHA-256 去重只留最后
一次成功发布号、失败发布排除、6 份窗口、GC 回收后仍可核对、重启恢复后
清单保留、Swagger/OpenAPI 文档。
