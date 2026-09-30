# 食品换线放行服务（共用产线过敏原裁决）

面向**共用产线**的食品换线放行：按产线顺序登记生产批次、所用原料批号及其过敏原、
成品声明过敏原、批次开工前清洁核验确认清除的过敏原；服务计算每批**实际可能含有的过敏原**、
**未声明项**，给出**放行 / 扣留结论及原因**，并支持原料批号与清洁核验更正后的**顺序重算**
与**可查询的历史裁决版本**。

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
python -m pytest -q            # 19 个用例：裁决、传递、更正重算、历史、原子性、产线隔离

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
├── examples/run_demo.py # 标准库实现的端到端调用示例
├── tests/test_service.py# pytest 测试（19 项）
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

所有写操作在单事务（`BEGIN IMMEDIATE`）内完成并进程内串行化，配合外键约束与 WAL，
保证更正与登记的原子性：校验失败即整体回滚，不会产生半成品批次或半成品重算版本。
