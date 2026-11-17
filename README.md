# 防汛雨量数据平台

把雨量站主、备翻斗计的**累计脉冲计数**转换为可信的 **UTC 小时降雨量**，供预警模型取数。
Flask + SQLite，网页上传/查询 + 等效 JSON API，Docker Compose 一键启动。

在此基础上提供**已供数登记与变化复核**：值班员把当前可供数的小时登记为“已供数”并保留
登记时雨量；后续补传/修订改变该小时时，它进入待复核清单（列原值与现值）；值班员核对确认后
离开清单。整点读数修订同时影响前后两小时。

## 快速开始

```bash
docker compose up --build
# 打开 http://localhost:8000
```

数据库文件保存在命名卷 `rainfall-data`（容器内 `/data/rainfall.db`），重启后数据、已供数登记
与待复核清单仍可查询。

本地开发（无需 Docker）：

```bash
pip install -r requirements.txt
python -m app.app                 # 开发服务器，监听 8000
# 或生产形态（与 Docker CMD 相同）：
gunicorn --bind 0.0.0.0:8000 --workers 1 --threads 8 "app.app:create_app()"
```

运行自测（45 个用例）：

```bash
python -m unittest discover -s tests -v
```

## 已供数登记与变化复核

### 使用流程（网页）

1. 上传某站的小时读数，打开 `/report?station=站点&hour=整点` 确认该小时“可供数”。
2. 点击“把本小时登记为已供数”。登记时雨量被永久保留（`registered_rainfall_mm`），
   同时作为确认基线（`baseline_rainfall_mm`）。
3. 之后若有补传/修订改变该小时雨量，首页与 `/reviews` 的**待复核清单**会列出该小时，
   显示原值（最近一次确认或登记时的雨量）、现值（当前由读数计算的雨量）与现值状态。
   雨量未变化的小时不进入清单。
4. 在 `/served/<id>` 核对“页面所见现值”，点击确认。确认后基线推进到现值，该小时离开清单。

登记是幂等的：同一（站点, 小时）重复登记不会覆盖原登记，也不会借登记绕过复核。
只有当前**可供数**（60 段齐全）的小时才能登记或确认。

### 一致性与并发保证

- **清单由数据推导**：不存“待复核”布尔位；待复核 ⇔ 当前小时雨量（脉冲总数）≠ 确认基线，
  或当前已不可供数。因此读数与清单不会出现半更新状态。
- **确认时二次校验**：确认在 `BEGIN IMMEDIATE` 事务中重新计算当前值，并要求
  （a）当前仍待复核，（b）提交的数据版本号与当前一致，（c）页面所见雨量与当前一致。
  任一项不满足即整体回滚、返回 409，登记保持待复核——即使期间的上传没有改变雨量合计
  （例如脉冲在相邻两段间转移），版本号也能识破。
- **数据版本号**：每次读数真正变化时，在写入读数的同一事务内递增受影响小时的
  `hour_versions`；整点分钟影响前后两小时，非整点分钟只影响所在小时。
- **原子性**：上传的读数、批次记录、行级明细、版本号在同一事务中提交，失败整体回滚；
  网页/API 每次请求内的多表读取使用只读事务快照，单页不混用新旧值。
- **网页与 API 同源**：两者都调用 `app/served.py` 的同一服务层函数。
- **持久化**：SQLite（WAL）落盘于挂载卷；新表通过幂等 `CREATE TABLE IF NOT EXISTS` 建立，
  旧数据库重启后自动补齐，不影响原有读数。

## CSV 格式

列顺序：`站点, 主备标记, UTC整分钟, 计数(0~9999), 修订号`；表头可选（支持中英文列名），无表头时按此顺序解析。

```csv
station,device,minute,counter,revision
ST01,primary,2026-11-17T10:00:00Z,9980,1
ST01,backup,2026-11-17T10:00:00Z,120,1
```

- **站点**：非空字符串（≤64 字符）。
- **主备标记**：`primary`/`backup`，也接受 `主`/`备` 等别名。
- **UTC 整分钟**：如 `2026-11-17T10:00:00Z`、`2026-11-17 10:00`；未带时区按 UTC 处理，带偏移会换算成 UTC；秒必须为 00。
- **计数**：整数 0~9999。
- **修订号**：非负整数。同一（站点, 设备, 分钟）**只接受更高修订号**，否则该行被拒绝并记入批次报告。

示例文件：`examples/sample.csv`（含完整小时、备计、回绕、缺口四种情形）。

## 计算规则

- 每小时 = 60 个相邻分钟间隔（整点 H:00 至下一整点，共 61 个端点）。
- 每个间隔：**主计两端齐全取主计差值，否则备计两端齐全取备计差值，绝不跨设备相减**；两端都无可用数据则该间隔缺失。
- 计数下降按一次 10000 回绕处理：差值 = (末 − 初) mod 10000。
- 每脉冲 0.2 mm；**任一间隔缺失则整小时不可供数**（`status=incomplete`，`rainfall_mm=null`），网页会标出缺口与每段来源。

## 接口

| 接口 | 说明 |
|---|---|
| `GET /` | 首页：待复核提示、登记入口、上传、查询、站点与批次列表 |
| `POST /upload` | 网页上传（文件或粘贴文本），返回批次报告页 |
| `POST /api/upload` | 同上，JSON 返回 |
| `GET /report?station=..&hour=..` | 网页小时报告（雨量、缺口、每段来源、已供数状态） |
| `GET /api/hourly?station=..&hour=..` | 等效 JSON（与网页同一计算入口，结果一致） |
| `POST /served` | 网页登记已供数（表单：`station`、`hour`） |
| `GET /served` | 已供数清单网页（可加 `?station=`） |
| `GET /served/<id>` | 已供数详情：登记原值、确认基线、现值、核对确认表单、轨迹 |
| `POST /served/<id>/confirm` | 网页确认（表单：`expected_version`、`expected_rainfall`） |
| `GET /reviews` | 待复核清单网页（可加 `?station=`） |
| `POST /api/served` | 登记已供数（JSON：`{"station":..,"hour":..}`；201 新建，200 已存在） |
| `GET /api/served?station=..` | 全部已供数登记（含 `needs_review`、原值/基线/现值） |
| `GET /api/served/<id>` | 单条登记 + 登记/确认轨迹 |
| `POST /api/served/<id>/confirm` | 确认（JSON：`{"expected_version":..,"expected_rainfall_mm":..}`；409 表示被拒绝） |
| `GET /api/reviews?station=..` | 待复核清单，仅雨量已变化的已供数小时 |
| `GET /api/stations` | 站点数据范围 |
| `GET /api/batches` | 上传批次列表 |
| `GET /batches/<id>` | 批次行级明细（接受/拒绝及原因） |
| `GET /api/health` | 健康检查 |

`hour` 需整点对齐，如 `2026-11-17T10:00Z`。预警模型取数时请检查 `status == "ok"` 再使用 `rainfall_mm`。

确认 API 必须带页面所见值与版本号（来自 `GET /api/served/<id>` 的
`current_version` / `current_rainfall_mm`），否则返回 400；版本或雨量与当前不一致返回 409。

## 一致性与可靠性设计

- **两小时一致**：小时雨量由 `readings` 表现算现算（无缓存），补传或修订整点读数提交后，
  前后两小时立即同时反映新值；网页与 API 共用同一计算函数。
- **原子性**：每批上传的读数、批次记录、行级明细、小时版本号在**单个事务**中写入，
  任何失败整体回滚，不留半批新结果。确认同样是单个事务：校验失败不产生任何更新。
- **持久化**：SQLite（WAL 模式）落盘于挂载卷，重启后读数、登记、待复核清单仍可查询。
- **并发**：单 gunicorn worker 多线程 + SQLite `BEGIN IMMEDIATE` 与 `busy_timeout`，
  上传与确认写入串行化；页面内多表读取走只读快照。

## 目录结构

```
app/
  core.py        # 纯逻辑：CSV 解析校验、小时雨量计算（不依赖 Flask/SQLite）
  db.py          # SQLite：模式、事务性批量入库、小时版本号、查询
  served.py      # 已供数登记、待复核清单、确认（网页与 API 共用）
  app.py         # Flask 路由（网页 + API）
  templates/     # 页面模板（含 served_list / served_detail / reviews）
tests/test_app.py  # 45 个自测用例
examples/sample.csv
Dockerfile / docker-compose.yml / requirements.txt
```
