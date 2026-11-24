# 轮对大修走行里程检修数仓供数 API

轮对换挂到另一节车厢后，检修数仓仍需累计它**大修之后**的走行里程。本服务用
FastAPI + SQLite 提供供数 API，支持：

- 分批报送**车厢 UTC 日走行米数**、**轮对在各车厢的装车日期段**（含首日、不含末日）、**大修日**；
- 按记录号（`record_no`）更正：同一记录号再次报送即为更正；
- 同一轮对装车段重叠时**整批退回**；更正与供数结果同一事务生效，失败保留旧结果；
- 按轮对 + 截止日查询：从截至该日**最近一次大修**算起（含大修日、含截止日），
  跨车厢累加；找不到大修返回 422 提示缺少起算日；
- 车厢日里程缺报时只给**已知里程下限**，并按车厢列出连续缺报日期段；零米视为已报；
- 查询**不扫描原始日里程表**；缺报日**不逐日建档**（由连续已报日块之间的空隙推导）。

## 目录结构

```
app/
  database.py   SQLite 连接与表结构（原始层 + 供数服务层）
  schemas.py    Pydantic 请求/响应模型
  service.py    批量报送事务、连续块维护、里程查询算法
  errors.py     领域错误（409 重叠/冲突，422 缺起算日）
  main.py       FastAPI 路由
tests/
  test_api.py   12 个端到端接口测试
Dockerfile, docker-compose.yml, requirements.txt
```

## 数据分层

| 层 | 表 | 作用 |
| --- | --- | --- |
| 原始供数层 | `car_daily_mileage` | 车厢-日里程，`record_no` 主键，可更正 |
| 原始供数层 | `wheelset_mount` | 装车段 `[start_day, end_day)`，半开区间 |
| 原始供数层 | `wheelset_overhaul` | 轮对大修日 |
| 供数服务层 | `car_mileage_day_serving` | 每个**已报**车厢-日一行（含零米），主键区间求和 |
| 供数服务层 | `car_mileage_block` | 连续已报日压缩块，块间空隙即缺报段，缺报日不建档 |

报送在单个 SQLite 事务内同时写原始表与服务层表；任何一步失败整体回滚，旧供数结果保留。
查询只读 `wheelset_overhaul`、`wheelset_mount` 与两张服务层表，不读
`car_daily_mileage`（有测试 `test_query_never_reads_raw_mileage_table` 用 SQLite
authorizer 禁止读取该表来验证）。

## 启动

### Docker Compose

```bash
docker compose up --build
# API: http://localhost:8000
# 交互式自测文档: http://localhost:8000/docs
# SQLite 数据持久化在命名卷 wheelset-data -> /data/warehouse.db
```

### 本地（无 Docker 时）

```bash
python3 -m venv --without-pip .venv          # 本机若有 pip 可直接 python3 -m venv .venv
# 按环境补装 pip 后：
.venv/bin/pip install -r requirements.txt
DB_PATH=./data/warehouse.db .venv/bin/uvicorn app.main:app --reload --port 8000
```

### 跑测试

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/ -v
```

## API 摘要

### `POST /ingest/batch`

三类记录都可选，分批报送；`record_no` 在各自表内命名，重复出现即更正。

```json
{
  "mileage": [
    {"record_no": "ML-1", "car_id": "C10", "day": "2026-01-20", "meters": 1200}
  ],
  "mounts": [
    {"record_no": "MT-1", "wheelset_id": "W7", "car_id": "C10",
     "start_day": "2026-01-20", "end_day": "2026-01-23"}
  ],
  "overhauls": [
    {"record_no": "OH-1", "wheelset_id": "W7", "overhaul_day": "2026-01-20"}
  ]
}
```

- 装车段为半开区间 `[start_day, end_day)`；同一轮对任意两段重叠 → `409`，整批退回。
- 不同 `record_no` 指向同一车厢-日 → 唯一约束冲突 `409`，整批退回。
- 响应给出收到/新增/更正条数及记录号清单。

### `GET /wheelsets/{wheelset_id}/mileage?cutoff_day=YYYY-MM-DD`

- 起算日：`overhaul_day <= cutoff_day` 中最近的一次大修；没有则 `422`
  （`detail` 说明缺少起算日）。
- 区间 `[大修日, cutoff_day]`（两端都含），按与该区间相交的装车段裁剪，
  跨车厢累加已报里程。
- 缺报时 `mileage_complete=false`、`is_lower_bound=true`，`known_meters` 仅为下限；
  `missing_ranges` 按车厢给出连续缺报半开段及天数。
- 轮对未装车的空档不计缺报；零米日算已报，不会出现在缺报段里。

## 在 /docs 自测移装与补报（推荐走查顺序）

1. `POST /ingest/batch`：报送大修 `OH-1`（W7 / 2026-01-20）、装车段 `MT-1`
   （W7 在 C10，01-20 至 01-23）和 C10 三天里程，其中一天 `meters=0`。
2. 再调一次 `POST /ingest/batch`：报送移装段 `MT-2`（W7 在 C20，01-23 至 01-26，
   与 MT-1 相邻不重叠），C20 只报 01-23、01-25，**故意漏报 01-24**。
3. `GET /wheelsets/W7/mileage?cutoff_day=2026-01-25`：看到跨车厢下限里程、
   `is_lower_bound=true`，C20 缺报段为 `[2026-01-24, 2026-01-25)`。
4. `POST /ingest/batch` 用新记录号补报 01-24：再查，里程变完整。
5. 用同一 `record_no` 再报一次不同米数：响应 `corrected` 计数增加，查询结果同步变化。
6. 报送一条与既有装车段重叠的记录：得到 `409`，且再次查询确认旧结果未变。
7. 查询一个没有大修的轮对：得到 `422` 起算日缺失提示。

## 已验证的测试场景（pytest，12 项）

- 单车完整周期、零米算已报、含大修日与截止日；
- 换车厢后半开装车段相邻不累报、跨车厢累加；
- 缺报给出下限与按车厢连续缺报段，零米日不被算作缺报；
- 新记录补报后里程变完整；
- 按记录号更正米数；更正到另一车厢/另一日时服务层日表与连续块同步迁移；
- 重叠装车段整批 409 退回且旧供数结果保留；车厢-日唯一冲突同样回滚；
- 无大修 / 大修晚于截止日 → 422；
- 二次大修后以最近一次大修为起算日；
- authorizer 禁止读原始日里程表时查询仍正常；
- 非法日期、负米数、倒挂装车段、批内重复记录号 → 422。
