# 快递理赔时限判定服务

按**订单创建时生效的政策版本**判定快递理赔申请资格的 HTTP 服务。输入订单、承诺时限、时区、节假日表与按时间到达的扫描事件，输出：

- 当前是否可申请（`eligible_to_file` / `overall_status`）；
- 各理赔类型的**截止时间**（当地时间 + UTC）；
- **缺失证据**清单；
- 逐条可审计的**判定依据**（含适用政策版本、起算事件、跳节假日说明、更正记录）。

## 判定规则

### 政策版本锁定

- 政策以 `valid_from / valid_to`（UTC，半开区间）入库；
- 下单时选取 `valid_from <= 订单创建时间 < valid_to` 的最新版本，版本号随订单持久化；
- 之后发布新政不影响既有订单（切换政策版本无需改代码，只更新数据）。

内置两个演示版本（见 `app/seed_data.py`）：

| 版本 | 生效区间（UTC） | 延迟送达 | 货物破损 | 货物丢失 |
|---|---|---|---|---|
| v1 | 2023-01-01 ～ 2025-01-01 | 7 个工作日 | 5 个自然日 | 30 个自然日 |
| v2 | 2025-01-01 起 | 10 个工作日 | 3 个工作日 | 15 个工作日 |

### 工作日与节假日

- 截止日按订单时区计算：`触发日次日起算 N 天，截止落在第 N 天的 23:59:59（当地时间）`；
- `business`（工作日）口径跳过周六日**和**节假日表中的 `holiday`；
- 节假日表中的 `workday`（调休补班日，即使是周末）**计为工作日**；
- 内置中国国务院办公厅 2024、2025 年放假/调休安排（72 个特殊日期）。

例：2025-02-11（周二）签收迟到，10 个工作日的截止为 **2025-02-25 23:59:59（Asia/Shanghai）**——其间春节假期（1/28–2/4 在起算前、无影响；本次推算仅跳过普通周末）。2025-10-01 起算则跳过国庆+中秋黄金周，截止 **2025-10-21**。

### 理赔类型与状态

三类理赔独立判定：

- `delay` 延迟送达：签收晚于承诺时限、或承诺时限已过仍无签收 → 构成；未到承诺时限无签收 → `pending`；准时签收 → `not_applicable`；订单未给承诺时限 → `indeterminate`；
- `damage` 货物破损：出现 `damage_reported`（破损申报）扫描即触发；
- `loss` 货物丢失：出现 `lost_confirmed`（丢件确认）扫描即触发。

每个类型状态：`filable`（可申请）/ `pending`（等待触发事件）/ `not_filable`（已过截止）/ `not_applicable`（不成立）/ `indeterminate`（证据不足不可判定）。顶层 `overall_status`：任一可申请即 `filable`，否则有 `pending/indeterminate` 即 `pending`，全部 `not_applicable` 为 `closed`。

### 事件流规则（重点）

1. **按到达时间顺序入库**：以 `arrived_at`（服务接收时间）为准，必须相对已接受流单调不减。倒序提交返回 `422 EVENT_ORDER_INVALID` 并标明出错下标，**整批回滚**。
2. **重放不重复计入**：`event_id` 在同一订单内唯一；重复提交进入 `duplicates`，不插入、不参与判定。
3. **时间字段必须带时区偏移**（如 `2025-02-11T10:00:00+08:00`），字段缺失/时间 naive 一律 `422 INVALID_REQUEST`。
4. **迟到更正不改写归档**：判定可 `POST .../archive` 归档（生成不可变 JSON 快照）。归档后到达的任何事件（含更正）会**开启新一代（generation）判定**，旧代快照原样保留；新结果通过 `supersedes_generation` 指向旧代。
5. **更正事件三种动作**（`is_correction` 语义由 `correction_action` 表达）：
   - `amend`：修正目标基础事件的发生时间（须带 `target_event_id`，类型须一致）；
   - `cancel`：撤销目标事件（如丢件确认被推翻）；
   - `insert`：补录一条此前漏掉的扫描（允许类型：signed / damage_reported / lost_confirmed）。
   更正只影响"物化"后的判定视图，原始事件行永不修改或删除。

## 目录结构

```
app/
  config.py        # 环境变量配置（数据目录、默认截止钟点）
  constants.py     # 事件/理赔类型与中文标签
  db.py            # SQLite 连接、建表、串行化写事务
  engine.py        # 纯函数核心：政策选取/工作日推算/事件物化/判定
  errors.py        # 业务错误码
  schemas.py       # Pydantic 入参模型与时区校验
  seed_data.py     # 政策版本 + CN 2024/2025 节假日种子
  services.py      # 订单、事件摄取、归档/开代、判定装配
  main.py          # FastAPI 路由
init_db.py         # 初始化入口（建表 + 种子，幂等）
examples/call_api.py  # 端到端调用示例（仅标准库）
examples/dry_run.py   # 无写入预演调用示例（成功批/倒序失败/归档后按新一代）
tests/             # pytest 引擎单测 + API 端到端测试（38 个用例）
Dockerfile, docker-compose.yml
```

## 快速启动

### 方式一：Docker Compose（推荐，自动初始化）

```bash
docker compose up --build
# 服务：http://localhost:8000  交互文档：http://localhost:8000/docs
```

数据持久化在命名卷 `claim-data`（容器内 `/data`，SQLite 单文件）。

### 方式二：本地 Python（3.11+）

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python init_db.py                       # 建表 + 写入政策与节假日（幂等）
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

重新初始化（清空重来）：停止服务后删除数据目录（默认 `./data`），再执行 `python init_db.py`。

### 运行调用示例

```bash
pip install -r requirements.txt          # 示例只用标准库，装不装均可
python examples/call_api.py              # BASE_URL 可改目标地址
python examples/dry_run.py               # 无写入预演三种场景
```

### 运行测试

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest -q
```

## HTTP API

所有请求/响应均为 JSON。错误统一形如：

```json
{"error": {"code": "EVENT_ORDER_INVALID", "message": "...", "details": {...}}}
```

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| POST | `/api/v1/orders` | 创建订单（按创建时间锁定政策版本） |
| GET | `/api/v1/orders/{id}` | 查询订单 |
| POST | `/api/v1/orders/{id}/events` | 提交单个扫描事件 |
| POST | `/api/v1/orders/{id}/events/batch` | 批量提交（顺序错误整批回滚） |
| GET | `/api/v1/orders/{id}/events` | 查看已接受事件流 |
| POST | `/api/v1/orders/{id}/events/dry-run?as_of=...` | **更正事件无写入预演**：不落事件/不开新代/不改快照，返回逐项校验与模拟判定差异 |
| GET | `/api/v1/orders/{id}/determination?as_of=...` | 当前判定（最新代已归档则返回快照） |
| POST | `/api/v1/orders/{id}/determination/archive?as_of=...` | 归档当前代判定 |
| GET | `/api/v1/orders/{id}/determinations` | 全部"代"的判定历史与快照 |
| GET | `/api/v1/policies` | 政策版本列表 |
| GET | `/api/v1/calendars/{code}` | 节假日表 |

`as_of` 为可选判定基准时刻（带偏移的 ISO8601），缺省取服务当前 UTC 时间——便于回溯测试。

### curl 示例

**1) 创建订单**

```bash
curl -s -X POST http://localhost:8000/api/v1/orders \
  -H 'Content-Type: application/json' \
  -d '{
    "order_id": "O-1001",
    "created_at": "2025-02-05T09:00:00+08:00",
    "timezone": "Asia/Shanghai",
    "calendar_code": "CN",
    "promise_by": "2025-02-10T18:00:00+08:00",
    "weight_kg": 2.5
  }'
```

**2) 提交签收扫描（晚于承诺时限）**

```bash
curl -s -X POST http://localhost:8000/api/v1/orders/O-1001/events \
  -H 'Content-Type: application/json' \
  -d '{
    "event_id": "EV-1",
    "event_type": "signed",
    "event_time": "2025-02-11T10:00:00+08:00",
    "arrived_at": "2025-02-11T10:05:00+08:00"
  }'
```

**3) 查询判定**

```bash
curl -s "http://localhost:8000/api/v1/orders/O-1001/determination?as_of=2025-02-20T12:00:00%2B08:00"
```

响应节选：

```json
{
  "order_id": "O-1001",
  "policy_version": "v2",
  "overall_status": "filable",
  "eligible_to_file": true,
  "earliest_deadline_local": "2025-02-25T23:59:59+08:00",
  "missing_evidence": [],
  "claims": [
    {
      "claim_type": "delay",
      "status": "filable",
      "deadline_local": "2025-02-25T23:59:59+08:00",
      "missing_evidence": [],
      "basis": [
        "实际签收 2025-02-11 10:00:00 CST 晚于承诺时限 2025-02-10 18:00:00 CST，构成延迟送达",
        "时限 10 个工作日（已跳过当地节假日），截止 2025-02-25 23:59:59 CST",
        "当前时间在申请时限内，可申请"
      ]
    }
  ],
  "basis": ["适用政策版本：v2（订单于 2025-02-05T09:00:00+08:00 创建）", "..."]
}
```

**4) 归档，再提交迟到更正（产生新一代，旧结果不变）**

```bash
curl -s -X POST "http://localhost:8000/api/v1/orders/O-1001/determination/archive?as_of=2025-02-20T12:00:00%2B08:00"

curl -s -X POST http://localhost:8000/api/v1/orders/O-1001/events \
  -H 'Content-Type: application/json' \
  -d '{
    "event_id": "EV-CORR-1",
    "event_type": "signed",
    "event_time": "2025-02-09T09:00:00+08:00",
    "arrived_at": "2025-02-21T08:00:00+08:00",
    "correction_action": "amend",
    "target_event_id": "EV-1",
    "note": "网点补传准确签收时间"
  }'

curl -s http://localhost:8000/api/v1/orders/O-1001/determinations
```

**5) 更正事件无写入预演（dry-run）**

在真正摄取前，用 `POST .../events/dry-run` 模拟整批更正的处理结果。请求体与 `/events/batch` 完全一致，`as_of` 可选（缺省取当前时刻）；**接口只读，不写入事件、不开启新一代、不改变归档快照**（响应中 `writes_performed=false`）。

```bash
curl -s -X POST "http://localhost:8000/api/v1/orders/O-1001/events/dry-run?as_of=2025-02-20T12:00:00%2B08:00" \
  -H 'Content-Type: application/json' \
  -d '{"events": [{
    "event_id": "EV-CORR-1",
    "event_type": "signed",
    "event_time": "2025-02-09T09:00:00+08:00",
    "arrived_at": "2025-02-21T08:00:00+08:00",
    "correction_action": "amend",
    "target_event_id": "EV-1",
    "note": "网点补传准确签收时间"
  }]}'
```

响应要点：

- `valid`：整批是否通过全部校验（到达顺序单调、更正目标存在且为基础事件、amend 类型一致、事件类型合法）；
- `items`：每项的校验结果，`status` 为 `accepted` / `duplicate`（与已落库或批内更早事件同 ID）/ `rejected`（首个失败项）/ `skipped`（首个失败之后的项）；
- `first_failure`：首个失败位置（`index`、`event_id`、错误码与明细），失败时 `simulated_determination` 与 `diff` 为 `null`，现状完整保留；
- `baseline_determination`：当前最新代事件视图在同一 `as_of` 上的判定（各理赔状态、截止时间、缺失证据、判定依据）；
- `simulated_determination`：整批事件按现有 amend/cancel/insert 规则物化后的模拟判定；
- `diff`：基线 → 模拟的判定依据差异，含 `overall_status` / `eligible_to_file` / 最早截止 / 顶层缺失证据与顶层 `basis` 增删，以及每个发生变化的理赔（状态、截止、锚点、缺失证据增删、依据是否变化）；
- `would_open_generation` / `simulated_generation_no`：若最新代**已归档**，模拟判定明确按新一代（`gen_no+1`，`supersedes` 指向旧代）计算，同时 `archived_snapshot` 原样回传不可变快照；预演本身不会真的开代。

业务校验失败仍是 HTTP 200 + `valid:false`（这是预演结果而非请求错误）；请求体格式问题（缺字段、naive 时间）仍为 `422 INVALID_REQUEST`，订单不存在为 `404 ORDER_NOT_FOUND`。现有摄取接口（`/events`、`/events/batch`）行为不变。

```bash
python examples/dry_run.py     # 成功批 / 倒序失败批 / 归档后按新一代预演 三种场景
```

## 设计取舍与扩展

- **存储**：SQLite + 进程内互斥锁，事件"顺序校验 + 插入"在单事务内完成；水平扩展或多副本时把 `app/db.py` 换成 Postgres（事件表加 `(order_id, arrived_at)` 约束即可）。
- **新增政策版本**：向 `policies` 表插入一行 JSON（时限天数、工作日/自然日、截止钟点均可配），无需发版；新增节假日在 `calendars` 表 upsert。
- **新增理赔类型/扫描事件**：在 `constants.py` 登记标签、政策 JSON 增规则、`engine.py` 增加一个 resolver，接口与代际机制无需改动。
- 截止钟点默认 23:59:59，可被政策窗口级 `cutoff` 或环境变量 `CLAIM_DEFAULT_CUTOFF` 覆盖。
