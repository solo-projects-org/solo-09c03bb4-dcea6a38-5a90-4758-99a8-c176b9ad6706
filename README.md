# 公交临时停运公告采集与查询服务

市政交通数据员从**分页公告源**采集公交临时停运信息，供导乘系统按 **UTC 时刻**查询停运站点。
本仓库包含两个服务，用 Docker Compose 一键启动：

| 服务 | 端口 | 作用 |
| --- | --- | --- |
| `api` | 8000 | 采集触发 + 停运查询 API（Swagger: <http://localhost:8000/docs>） |
| `source` | 8001 | 可切换版本的本地分页公告源（场景可热切换） |

数据保存在 SQLite 卷 `transit-data` 中，容器重启后仍可查询已发布结果。

---

## 1. 快速开始

```bash
docker compose up -d --build
curl -s http://localhost:8000/health
curl -s http://localhost:8001/health

# 触发一次采集（默认场景 v1）
curl -s -X POST http://localhost:8000/api/v1/collect | python3 -m json.tool

# 按 UTC 查询 STOP-001 在 2026-11-17T09:00:00Z 的停运公告
curl -s 'http://localhost:8000/api/v1/announcements?stop_id=STOP-001&at=2026-11-17T09:00:00Z' | python3 -m json.tool
```

Swagger UI: <http://localhost:8000/docs>，OpenAPI: <http://localhost:8000/openapi.json>。

不装 Docker 也可以直接跑（依赖见 `requirements.txt`）：

```bash
python3 -m uvicorn mock_source.server:app --port 8001 &
DATABASE_PATH=./data/transit.db SOURCE_BASE_URL=http://127.0.0.1:8001 \
  python3 -m uvicorn app.main:app --port 8000
```

## 2. API

### `POST /api/v1/collect` — 采集并发布

先从源端第 1 页取得**快照标识** `snapshot_id`，再用同一个 `snapshot_id` 逐页拉取
`GET /announcements?page=N&snapshot=...` 的第 2..`total_pages` 页，校验后原子发布。

| HTTP | `status` | 含义 |
| --- | --- | --- |
| 200 | `published` | 新版本已发布并成为现行供数 |
| 200 | `already_current` | 同版重试；内容逐条一致，不重复发布 |
| 409 | `failed` | 版本回退 / 同号不同内容；旧供数保持不变 |
| 502 | `failed` | 源不可达、缺页、跨页换版/换快照、总页数不符、同号内容冲突、源重启导致快照失效等；旧供数保持不变 |

响应中的 `snapshot_id` 是本次整批页所读取的源快照，便于排查。

成为现行公告的**前提**（任一不满足即整批拒绝）：

1. 第 1 页声明 `total_pages`，实际取到 1..`total_pages` 全部页；
2. 所有页的 `snapshot_id`、`source_version`、`source_revision`、`total_pages` 完全一致；
3. 全部页拉完后才校验 — 没有“边拉边发”；
4. 同一 `announcement_no` 若重复出现，内容必须完全一致（逐条去重）；内容不同则整批失败。

第 2 条里的 `snapshot_id` 是关键：**两个场景即使沿用相同的
`source_version`、`source_revision` 和页数，也会拿到不同的快照标识**，因此
“第 1 页来自旧场景、第 2 页来自新场景”的拼接一定会被拒绝，不会被当成一个从未
公布的公告版发布出去。

发布是单事务：`published_version` + `published_announcement` + `current_version` 一起提交，
因此任何时刻查询看到的都是同一版本的完整集合，不会出现半批数据。

### `GET /api/v1/announcements?stop_id=...&at=...`

- `at` 为 ISO-8601 UTC 时刻，`Z` 或 `+00:00` 均可；**无时区后缀按 UTC 解释**；省略则为当前时刻。
- 时间窗为**闭区间**：`start_utc <= at <= end_utc`。
- 只查**现行版本**；返回命中的每条公告号、站点号、停运时段，以及 `source_version` / `source_revision`。
- 未发布过任何版本时返回 404；`at` 非法返回 422。

```json
{
  "stop_id": "STOP-001",
  "at_utc": "2026-11-17T09:00:00Z",
  "source_version": "2026-11-17T06:00:00Z",
  "source_revision": 1,
  "announcements": [
    {
      "announcement_no": "A-1001",
      "stop_id": "STOP-001",
      "start_utc": "2026-11-17T08:00:00Z",
      "end_utc": "2026-11-17T12:00:00Z"
    }
  ]
}
```

### 其他

- `GET /api/v1/current-version` — 现行版本及其页数/条数/发布时间（未发布时 404）。
- `GET /api/v1/versions` — 已发布过的版本列表（供排查，按 revision 倒序）。
- `GET /health` — 存活检查 + 现行版本号。

## 3. 换版语义

- **新版整版替换**：v2 中不再出现的公告（如 `A-1001`）从查询结果中消失。
- **同站其他有效公告保留**：只要公告仍在现行版本中（例如 `A-1003` 属于 `STOP-002`），换版后照常命中。
  换版以“整版”为单位，不做逐条差量合并，因此不会误删仍在供数中的公告。
- **版本只能前进**：候选 `source_revision` 必须严格大于现行 revision。
  一个先启动、后完成的旧版采集器提交时会被 409 拒绝——**旧版后完成不能覆盖新版**。
- **同版重试幂等**：同 `source_version` 且内容哈希一致 → `already_current`，不新增 `published_version` 行，
  不改变 `published_at`；若该版本已被更新版取代，则拒绝回滚（409）。
  源端重启或再次切换后内容相同的新快照也走这条路径，不会重复发布。
- **同号冲突**：同 `source_version` 但内容哈希不同 → 409；同一批页内同号不同内容 → 502。
- **快照交接**：场景切换不影响已在途的采集。旧采集继续读取它第 1 页拿到的快照直到读完；
  切换后的新采集立即读到新场景（新快照）。若期间源进程重启，旧快照不再存在，
  续页请求返回 410，采集端整批失败（502）并保留原供数，绝不发布新旧混合页。

## 4. 可切换的本地公告源

`source` 服务从 `mock_source/scenarios/*.json` 加载场景，默认 `v1`。每个场景含
`source_version`、`source_revision`、`page_size`、`announcements`，并可带故障模式。
每次切换都会冻结一份**不可变快照**并分配新的 `snapshot_id`：

| 场景 | 作用 |
| --- | --- |
| `v1` / `v2` / `v3` | 正常分页版本（2 条/页），用于换版验证 |
| `handoff_a` / `handoff_b` | 版本、revision、页数**完全相同**但内容不同，用于验证快照交接与同号冲突 |
| `handoff_a_slow` | 每页延迟 0.4s 的 `handoff_a`，用于“采集中途切换场景”验证 |
| `mixed_versions` | 第 2 页换 `source_version` → 跨页换版 |
| `missing_page` | 第 2 页 404 → 缺页 |
| `short_pages` | 首页声明的页数比实际多 → 页集不完整 |
| `duplicate_conflict` | 第 2 页重复第 1 页公告号但内容不同 |
| `old_revision` | revision 1，用于验证版本回退被拒 |
| `slow_v2` | 每页延迟 3s 的 v2，用于制造“旧版后完成” |
| `fast_v3` | 无延迟的 v3 |

分页交接协议：

```text
GET /announcements?page=1                       -> 200, {"snapshot_id": "…", ...}
GET /announcements?page=2&snapshot=<id>         -> 200, {"snapshot_id": "…", ...}
GET /announcements?page=2                       -> 400  (缺少快照标识)
GET /announcements?page=2&snapshot=<old-id>     -> 410  (源已重启或快照被淘汰)
```

- 快照在 `POST /scenario` 时冻结，切换只影响之后的第 1 页请求；在途页集继续读原快照。
- 快照 id 含随机 per-process boot id，源重启后旧 id 一定未知（410），
  不会误命中重启后的新数据。
- 最多保留 `SNAPSHOT_HISTORY`（默认 128）份快照，超出后最旧的被淘汰，续页同样得到 410。
- `GET /snapshots` 可查看当前与历史快照；`GET /health` 回显当前 `snapshot_id`。

切换方式（无需重启）：

```bash
curl -X POST http://localhost:8001/scenario -H 'content-type: application/json' -d '{"name":"v2"}'
curl -s http://localhost:8001/scenarios        # 列出全部场景与当前场景
```

并发验证还可以用**按 URL 固定场景**的入口，让两个采集器同时面对不同版本：

```bash
# 采集器 A（慢，v2）
SOURCE_BASE_URL=http://source:8001/pinned/slow_v2 python3 -m uvicorn app.main:app --port 8000
# 采集器 B（快，v3，共用同一个 SQLite 文件）
SOURCE_BASE_URL=http://source:8001/pinned/fast_v3 python3 -m uvicorn app.main:app --port 8003
```

场景目录以只读方式挂载进容器，新增 `xxx.json` 后调用 `POST /scenario {"name":"xxx"}` 即可。

## 5. 自测

```bash
python3 -m pytest tests -q                    # 换版/缺页/冲突/快照交接/并发/重启等
python3 scripts/smoke.py                      # 对运行中的 source+api 做端到端检查
API=http://127.0.0.1:8000 SOURCE=http://127.0.0.1:8001 scripts/restart_check.sh
```

测试覆盖点：

- 完整页集成为查询依据；闭区间边界；无时区时间按 UTC。
- 换版后缺失公告撤下、同站/他站仍有效的公告保留。
- 跨页换版、缺页、总页数不足、同号内容冲突、源不可达 → 失败且旧供数不变。
- 同版重试不重复发布；同号不同内容、旧 revision、已发布旧版回滚 → 拒绝。
- 多线程/多进程并发：慢旧版后完成被 409 拒绝，新版保持现行。
- **快照交接**：相同版本标签的两个场景之间切换时，在途采集读完原快照、新采集立即读新快照；
  跨页快照不一致、源重启导致快照失效 → 整批 502 且旧供数不变；同标签不同内容 → 409；
  源端快照 id 在重启/淘汰后返回 410，且不会与重启后的新快照混淆。
- 重启（新建 app 实例 / `docker compose restart api`）后仍可查询。

## 6. 目录结构

```
app/                 API 服务
  models.py          公告/页模型、UTC 归一化、内容哈希、重复号校验
  db.py              SQLite schema、事务发布、查询
  collector.py       快照绑定分页拉取 + 完整性校验
  main.py            FastAPI 路由（Swagger 见 /docs）
mock_source/         可切换分页公告源
  server.py          不可变快照、正常 + 故障场景服务
  scenarios/*.json   场景数据
scripts/             smoke.py、restart_check.sh
tests/               pytest 用例（含快照交接 test_handoff.py）
```

## 7. 已知边界

- 采集是**同步触发**（`POST /api/v1/collect` 阻塞到发布完成）。没有后台定时采集或任务队列。
- 多副本横向扩展时，各副本共享同一 SQLite 文件（`BEGIN IMMEDIATE` + 30s busy timeout 串行化写入）；
  如果改成多主机部署，需要换成 PostgreSQL 等支持跨机事务的存储。
- 公告只保留“版本 → 公告”关系，不提供历史版本查询接口（`/api/v1/versions` 只列版本元数据）。
- 源端快照保存在内存中，条数受 `SNAPSHOT_HISTORY` 限制。采集耗时若超过“切换 N 次或源重启”
  的窗口，续页会得到 410 并整批失败——这是有意的失败关闭，不会退化为混合发布。
- 源端快照是**进程内**状态；`source` 服务必须以单进程（`uvicorn --workers 1`，Compose 默认）
  运行。若水平扩成多 worker，第 2 页可能落到另一个没有该快照的进程上并得到 410
  （不会混合发布，但采集会失败），需要改为共享快照存储。
- 采集端对**不带 `snapshot_id`** 的旧源仍按版本/revision/页数一致性校验（向后兼容模式）；
  这种模式无法区分“标签相同、内容不同”的两个场景，只有配合快照源才能获得完整交接保证。
