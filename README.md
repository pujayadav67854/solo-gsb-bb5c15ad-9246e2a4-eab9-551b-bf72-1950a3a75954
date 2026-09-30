# 食品换线放行服务（共用产线过敏原裁决）

面向**共用产线**的食品换线放行：按产线顺序登记生产批次、所用原料批号及其过敏原、
成品声明过敏原、批次开工前清洁核验确认清除的过敏原；服务计算每批**实际可能含有的过敏原**、
**未声明项**，给出**放行 / 扣留结论及原因**，并支持原料批号与清洁核验更正后的**顺序重算**
与**可查询的历史裁决版本**。出货时可提交**出货核验单**关联多个已登记批次，
逐批固化裁决版本快照并原子确认；所含批次更正重算时已确认单自动转待复核、
重新核验后方可再次确认。

- 技术栈：Python 3.11 + FastAPI + SQLite（标准库 `sqlite3`，WAL 模式）
- 数据落盘：单个 SQLite 文件，Docker 卷持久化
- 一键启动：`docker compose up`（首次启动自动建表、写入过敏原字典与演示原料批号）

---

## 1. 业务规则

同一产线 `line_id` 上，批次按登记顺序获得递增序号 `seq`，对第 N 批：

```
带入  = 上一批（第 N-1 批）实际可能含有的过敏原
残留  = 带入 − 本次清洁核验“确认清除”的过敏原     # 只清除被确认覆盖的项目
本批  = 本批所用全部原料批号过敏原的并集
实际  = 残留 ∪ 本批
未声明 = 实际 − 成品声明过敏原
结论  = 未声明非空 → HOLD（扣留）；否则 RELEASE（放行）
```

要点：

- **清洁只清除被确认覆盖的过敏原**：清洁清单中未列出的残留过敏原继续留在产线。
- **上一批即使被扣留（HOLD），其实际过敏原仍继续向下传递**。
- 清洁发生在本批开工前，只能清除“上一批留下的”残留；本批原料再次引入的过敏原仍计入实际。
- **更正原料批号或某批清洁/声明/用料后**：从受影响的最早批次开始，按产线顺序重算该批及
  之后的全部批次（原料批号更正会覆盖所有引用它的产线）；每次重算追加一个**只增不改的历史
  裁决版本**，可随时查询。
- 更正接口为**全量替换**语义（传入的数组代表更正后的完整集合，不传的字段保持不变）。

### 出货核验单

一张出货核验单（`doc_no` 唯一）关联**多个已登记生产批次**，保存逐批“当时的裁决版本
与结论”，并支持确认与重算联动：

- **提交核验**：传入非空、无重复的批次号列表；逐批保存**当前裁决版本与结论**快照，
  单据初始为 `DRAFT`。未知批次、重复单号、列表内批次重复一律拒绝，
  **整事务回滚，不留下部分单据**。
- **草稿重新核验**：`POST /api/shipment-docs/{doc_no}/verify` 按各批次当前裁决
  刷新逐批快照（草稿、待复核单据可用）。
- **确认**：原子校验“逐批快照版本仍是当前版本”且“当前结论全部为 `RELEASE`”；
  通过则单据 `CONFIRMED` 并**追加保留一份确认快照（历次全部保留）**。不符则
  保持未确认（`DRAFT`/`REVIEW_PENDING`），返回逐批不符批次与原因：
  - `STALE_SNAPSHOT`：快照版本落后于当前版本（可附带 `HOLD_NOT_RELEASABLE`）
  - `HOLD_NOT_RELEASABLE`：当前结论不是 RELEASE —— **HOLD 批次始终不能确认**
- **重算联动**：已确认单据所含批次因批次/原料更正重算产生新裁决版本时，
  **立即转为 `REVIEW_PENDING`（待复核）——即使新结论仍为 RELEASE**；草稿单据不联动。
  待复核单据**必须先重新核验**（否则确认返回 `REVERIFY_REQUIRED`），
  刷新后校验通过才能再次确认；确认成功后回到 `CONFIRMED`，历史确认快照只增不删。
- **查询**：返回单据状态、逐批的快照版本/当前版本/是否落后（`stale`）/变化原因
  （如“批次更正重算（CORRECT_BATCH），结论由 RELEASE 变为 HOLD”），
  以及 `reverify_required` 标记；历次确认快照可单独查询。
- 现有批次裁决、更正与历史查询行为完全兼容，出货核验不改动任何批次裁决数据。

拒绝规则（失败时整个请求回滚，**不留下部分改动**）：

| 场景 | HTTP | error.code |
| --- | --- | --- |
| 批次号重复 | 409 | `DUPLICATE_BATCH` |
| 原料批号重复登记 | 409 | `DUPLICATE_MATERIAL_LOT` |
| 批次引用了未登记的原料批号 | 400 | `UNKNOWN_MATERIAL_LOT` |
| 过敏原代码不在受控字典内 | 400 | `INVALID_ALLERGEN` |
| 字段为空/类型错误等负载问题 | 400/422 | `INVALID_PAYLOAD` / FastAPI 校验 |
| 更正目标批次/原料批号不存在 | 404 | `CORRECTION_TARGET_NOT_FOUND` |
| 查询不存在的批次/原料批号 | 404 | `BATCH_NOT_FOUND` / `MATERIAL_LOT_NOT_FOUND` |
| 出货核验单号重复 | 409 | `DUPLICATE_SHIPMENT_DOC` |
| 出货单批次号列表为空或含重复 | 422 / 409 | FastAPI 校验 / `DUPLICATE_BATCH_IN_LIST` |
| 出货单引用未登记批次 | 400 | `UNKNOWN_BATCH` |
| 出货核验单不存在 | 404 | `SHIPMENT_DOC_NOT_FOUND` |
| 确认时快照过期或含 HOLD 批次 | 409 | `VERIFICATION_MISMATCH`（明细 `error.mismatches`） |
| 待复核单据未重新核验就确认 | 409 | `REVERIFY_REQUIRED` |
| 对已确认单据执行重新核验 | 409 | `INVALID_DOC_STATUS` |

过敏原受控字典采用欧盟 14 类法定过敏原代码（仅接受这些代码）：

`gluten`（含麸质谷物）、`crustaceans`（甲壳类）、`egg`（蛋）、`fish`（鱼）、
`peanut`（花生）、`soy`（大豆）、`milk`（乳）、`tree_nuts`（坚果）、`celery`（芹菜）、
`mustard`（芥末）、`sesame`（芝麻）、`sulphites`（亚硫酸盐）、`lupin`（羽扇豆）、
`molluscs`（软体动物）。完整清单可通过 `GET /api/allergens` 查询。

---

## 2. 一键启动（Docker Compose，推荐）

```bash
docker compose up --build
# 后台运行：docker compose up -d --build
# 停止并保留数据：docker compose down
# 停止并删除数据卷（彻底重置）：docker compose down -v
```

容器首次启动自动完成：建表 → 写入 14 项过敏原字典 → 写入 4 个演示原料批号与示例产线 `L1`。
数据保存在命名卷 `changeover-data`（容器内 `/data/changeover.db`）。

## 3. 本地直接运行（不使用 Docker）

```bash
pip install -r requirements.txt

# 可选：初始化空库时写入演示原料批号与产线 L1
export SEED_DEMO_DATA=1

python -m app
```

本地默认数据库文件为仓库内 `data/changeover.db`（自动创建目录）。

## 4. 运行测试与示例

```bash
pip install pytest httpx
python -m pytest -q            # 42 个用例：裁决/传递/更正重算/历史/原子性/产线隔离/出货核验全生命周期

# 服务启动后，运行端到端调用示例（标准库 urllib，无需额外依赖）
python examples/run_demo.py
BASE_URL=http://127.0.0.1:8080 python examples/run_demo.py   # 显式指定地址
```

---

## 5. 配置（环境变量）

| 变量 | 默认值（本地） | 容器默认 | 说明 |
| --- | --- | --- | --- |
| `HOST` | `0.0.0.0` | `0.0.0.0` | 监听地址 |
| `PORT` | `8080` | `8080` | 监听端口 |
| `DB_PATH` | `./data/changeover.db` | `/data/changeover.db` | SQLite 数据库文件路径 |
| `SEED_DEMO_DATA` | `0`（关） | `1`（开） | 仅在**全新空库**初始化时写入演示原料批号/产线；对已有库幂等、不重复写入 |

## 6. 服务地址与交互式文档

启动后默认地址端口：**http://127.0.0.1:8080**

- 健康检查：`GET /health`
- Swagger 交互文档：http://127.0.0.1:8080/docs
- OpenAPI 描述：http://127.0.0.1:8080/openapi.json

---

## 7. HTTP API 一览

### 参考数据

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/allergens` | 过敏原受控字典 |
| POST | `/api/material-lots` | 登记原料批号（lot_no、material、allergens） |
| GET | `/api/material-lots` | 列出全部原料批号 |
| GET | `/api/material-lots/{lot_no}` | 查询单个原料批号 |
| PUT | `/api/material-lots/{lot_no}` | **更正原料批号**（名称/过敏原），重算受影响批次及后续批次 |
| GET | `/api/lines` | 产线列表及各线批次数量 |

### 批次与裁决

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/batches` | **按产线顺序登记批次**，立即返回裁决（seq 自动取该线下一号） |
| GET | `/api/batches?line_id=L1` | 列出批次（可按产线过滤） |
| GET | `/api/batches/{batch_no}` | 查询批次及当前裁决快照 |
| PUT | `/api/batches/{batch_no}` | **更正批次**（用料/声明/清洁，任一可选），重算本批及后续批次 |
| PUT | `/api/batches/{batch_no}/cleaning` | 专用：仅更正开工前清洁核验确认清除的过敏原 |
| GET | `/api/batches/{batch_no}/history` | 查询该批**历史裁决版本**（只追加，含触发原因与时间） |

### 出货核验单

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/shipment-docs` | 提交核验单（`doc_no` + 非空无重复的 `batch_nos`），保存逐批当前裁决快照，返回单据快照（201） |
| GET | `/api/shipment-docs?status=DRAFT` | 列出核验单（可按 `DRAFT` / `CONFIRMED` / `REVIEW_PENDING` 过滤） |
| GET | `/api/shipment-docs/{doc_no}` | 单据状态 + 逐批记录版本/当前版本/`stale`/变化原因 |
| POST | `/api/shipment-docs/{doc_no}/verify` | 草稿重新核验 / 待复核单据重新核验：刷新逐批快照 |
| POST | `/api/shipment-docs/{doc_no}/confirm` | 原子确认；不符返回逐批 `mismatches` 并保持未确认 |
| GET | `/api/shipment-docs/{doc_no}/confirmations` | 历次确认快照（只追加，全部保留） |

核验单状态：`DRAFT`（草稿）→ `CONFIRMED`（已确认）→（所含批次重算）→
`REVIEW_PENDING`（待复核）→（重新核验后确认）→ `CONFIRMED`。

### 核验单响应字段

```jsonc
{
  "doc_no": "SH-20260930-01",
  "status": "REVIEW_PENDING",            // DRAFT / CONFIRMED / REVIEW_PENDING
  "batch_count": 2,
  "verified_at": "2026-09-30T08:00:00+00:00",
  "confirmed_at": "2026-09-30T08:01:00+00:00",
  "review_pending_at": "2026-09-30T08:05:00+00:00",
  "reverify_required": true,             // 待复核时：是否须先重新核验
  "review_reason": {                     // 触发待复核的首个变化批次
    "trigger": "CORRECT_BATCH", "reason": "批次更正重算",
    "batch_no": "B2026003", "current_version": 3, "current_disposition": "RELEASE"
  },
  "items": [
    {
      "position": 1, "batch_no": "B2026003", "line_id": "L1", "seq": 3,
      "snapshot_version": 2, "snapshot_disposition": "RELEASE",  // 核验当时
      "current_version": 3,  "current_disposition": "RELEASE",   // 当前裁决
      "stale": true,                                             // 快照已过期
      "change_reason": "批次更正重算（CORRECT_BATCH）"             // 过期原因
    }
  ]
}
```

确认失败（409）时 `error.mismatches` 逐批给出原因，例如：

```jsonc
{ "error": {
    "code": "VERIFICATION_MISMATCH",
    "message": "快照版本已过期或存在 HOLD 批次，单据保持未确认",
    "mismatches": [
      { "batch_no": "B2026003",
        "snapshot_version": 2, "current_version": 3,
        "snapshot_disposition": "RELEASE", "current_disposition": "HOLD",
        "reasons": ["STALE_SNAPSHOT", "HOLD_NOT_RELEASABLE"],
        "change_reason": "原料批号更正重算（CORRECT_LOT），结论由 RELEASE 变为 HOLD" }
    ]
} }
```

### 裁决响应字段

```jsonc
{
  "batch_no": "B2026003", "line_id": "L1", "seq": 3,
  "material_lots": [],
  "declared_allergens": [],
  "cleaned_allergens": [],
  "adjudication": {
    "version": 1,
    "actual_allergens": ["peanut"],      // 实际可能含有的过敏原
    "undeclared_allergens": ["peanut"],  // 未声明项（非空即扣留）
    "disposition": "HOLD",               // RELEASE / HOLD
    "reasons": [ /* 逐条原因，含过敏原与来源说明 */ ],
    "sources": { "peanut": ["上一批残留在共用产线、本次清洁未确认清除"] },
    "decided_at": "2026-09-30T00:00:00+00:00"
  }
}
```

更正接口额外返回 `recomputed`：本次按序重算的批次裁决摘要列表；
原料批号更正会列出各产线受影响批次。

---

## 8. curl 调用示例

```bash
# 1) 登记原料批号
curl -s -X POST http://127.0.0.1:8080/api/material-lots \
  -H 'Content-Type: application/json' \
  -d '{"lot_no":"RM-MILK-002","material":"全脂奶粉","allergens":["milk"]}'

# 2) 登记第 1 批：牛奶巧克力，声明 milk —— 放行
curl -s -X POST http://127.0.0.1:8080/api/batches \
  -H 'Content-Type: application/json' \
  -d '{"batch_no":"B001","line_id":"L1","material_lots":["RM-MILK-002"],
       "declared_allergens":["milk"],"cleaned_allergens":[]}'

# 3) 登记第 2 批：使用含花生的原料、只清洁 milk、只声明 milk —— peanut 未声明，扣留
curl -s -X POST http://127.0.0.1:8080/api/batches \
  -H 'Content-Type: application/json' \
  -d '{"batch_no":"B002","line_id":"L1","material_lots":["RM-NUT-001"],
       "declared_allergens":["milk"],"cleaned_allergens":["milk"]}'

# 4) 第 3 批无过敏原原料且未清洁 —— 继承上批（被扣留批）残留 peanut，扣留
curl -s -X POST http://127.0.0.1:8080/api/batches \
  -H 'Content-Type: application/json' \
  -d '{"batch_no":"B003","line_id":"L1","material_lots":[],
       "declared_allergens":[],"cleaned_allergens":[]}'

# 5) 更正 B003 开工前清洁核验：确认清除 peanut —— B003 及其后续批次自动重算
curl -s -X PUT http://127.0.0.1:8080/api/batches/B003/cleaning \
  -H 'Content-Type: application/json' \
  -d '{"cleaned_allergens":["peanut"]}'

# 6) 更正原料批号过敏原（例如复检发现还含 sesame）—— 所有引用批次按线重算
curl -s -X PUT http://127.0.0.1:8080/api/material-lots/RM-NUT-001 \
  -H 'Content-Type: application/json' \
  -d '{"allergens":["peanut","sesame"]}'

# 7) 查询历史裁决版本
curl -s http://127.0.0.1:8080/api/batches/B003/history

# 8) 提交出货核验单：唯一单号 + 非空、无重复的已登记批次号列表
curl -s -X POST http://127.0.0.1:8080/api/shipment-docs \
  -H 'Content-Type: application/json' \
  -d '{"doc_no":"SH-001","batch_nos":["B001","B003"]}'

# 9) 原子确认（快照版本须仍为当前版本且全部 RELEASE；HOLD 批次始终拒绝）
curl -s -X POST http://127.0.0.1:8080/api/shipment-docs/SH-001/confirm

# 10) 查询核验单：状态 + 逐批记录版本/当前版本/变化原因
curl -s http://127.0.0.1:8080/api/shipment-docs/SH-001

# 11) 更正批次（即使结论仍为 RELEASE）：已确认单立即转待复核，然后重新核验
curl -s -X PUT http://127.0.0.1:8080/api/batches/B003/cleaning \
  -H 'Content-Type: application/json' \
  -d '{"cleaned_allergens":["peanut"]}'
curl -s -X POST http://127.0.0.1:8080/api/shipment-docs/SH-001/verify
curl -s -X POST http://127.0.0.1:8080/api/shipment-docs/SH-001/confirm   # 再次确认

# 12) 历次确认快照（只追加，全部保留）
curl -s http://127.0.0.1:8080/api/shipment-docs/SH-001/confirmations
```

更多可运行示例见 [`examples/run_demo.py`](examples/run_demo.py)（包含完整的
登记 → 扣留传递 → 清洁更正重算 → 历史查询 → 各类拒绝场景）。
示例脚本可重复调用其中的“登记原料批号”接口（重复时自动跳过）；批次号固定，
重复运行前如需重置可 `docker compose down -v` 后重新启动，或删除本地 `data/` 目录。

---

## 9. 项目结构

```
.
├── app/
│   ├── config.py        # 环境变量配置（DB_PATH / HOST / PORT / SEED_DEMO_DATA）
│   ├── allergens.py     # 14 项过敏原受控字典
│   ├── errors.py        # 业务错误（code + HTTP 状态码）
│   ├── db.py            # SQLite 连接、建表、事务、初始化/种子
│   ├── service.py       # 核心规则：传递/清洁/裁决、更正重算、历史版本
│   ├── schemas.py       # 请求模型
│   ├── main.py          # FastAPI 路由
│   └── __main__.py      # python -m app 入口
├── examples/run_demo.py # 标准库实现的端到端调用示例（含出货核验单生命周期）
├── tests/test_service.py  # 批次裁决测试（20 项）
├── tests/test_shipment.py # 出货核验单测试（22 项）
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 10. 数据库表（SQLite）

- `allergen`：过敏原字典
- `material_lot`：原料批号 → 名称、过敏原集合
- `line` / `batch` / `batch_material`：产线、批次（含 `line_id+seq` 唯一顺序）、批次用料关联
- `adjudication`：每批当前裁决快照
- `adjudication_version`：只追加的历史裁决版本（`REGISTER` / `CORRECT_BATCH` / `CORRECT_LOT`）
- `shipment_doc`：出货核验单（`DRAFT` / `CONFIRMED` / `REVIEW_PENDING`，含待复核原因）
- `shipment_doc_item`：核验单逐批快照（核验当时的裁决版本与结论、提交顺序）
- `shipment_doc_confirmation`：只追加的历次确认快照

所有写操作在单事务（`BEGIN IMMEDIATE`）内完成并进程内串行化，配合外键约束与 WAL，
保证更正与登记的原子性：校验失败即整体回滚，不会产生半成品批次或半成品重算版本。
出货核验单的提交/核验/确认同样在单事务内完成：未知批次、重复单号/批次或确认不符
都会整体回滚，不留下部分单据或部分快照；批次重算在同一事务内联动将已确认单转待复核。

> 已有数据库平滑升级：建表 DDL 使用 `CREATE TABLE IF NOT EXISTS`，重启服务后
> 自动追加出货核验相关表，不影响既有数据与裁决版本。
