# 提花织带纹版修复 + 裁样 API

根据同系列**参考 PNG** 与用透明像素标注缺损的**残片 PNG**，补出可反复织造的
重复单元纹版；再凭草案编号开**裁样单**，在平铺平面上从成品织带上剪下一朵
完整的花并保全邻花。技术栈：FastAPI + Pillow + OR-Tools CP-SAT（含内置回溯兜底）
+ SQLite。

## 目录结构

```
.
├── app/
│   ├── images.py    # PNG 解析/校验、周期投影、2×2 图块统计
│   ├── solver.py    # CP-SAT 可行性 + 局部搜索优化 + 回溯兜底
│   ├── cutting.py   # 裁样：跨单元花朵追踪、最小面积矩形、刀位校验
│   ├── storage.py   # 草案与裁样单 SQLite 持久化
│   └── main.py      # FastAPI 路由
├── tests/
│   ├── test_app.py       # 26 个纹版修复单元/端到端测试
│   └── test_cutting.py   # 28 个裁样单元/端到端测试
├── demo/
│   ├── make_demo_data.py   # 生成演示 PNG
│   ├── demo_requests.sh    # 可直接运行的 curl 演示（含裁样单）
│   └── *.png               # 演示图片
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 快速开始（Docker Compose）

```bash
docker compose up --build -d
# 健康检查
curl http://127.0.0.1:8000/health
# 跑全套演示请求（创建草案、补录、冲突、领取 PNG、开裁样单、登记下刀、改刀位）
bash demo/demo_requests.sh
```

SQLite 数据库通过卷挂载持久化在 `./data/drafts.db`（草案与裁样单同库）。

## 本地运行（无 Docker）

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
# 交互式文档：http://127.0.0.1:8000/docs
```

## 约束与规则

- 两张图每边至多 **64 像素**；参考图最多 **4 色**且必须完全不透明。
- 残片透明像素（alpha < 128）表示缺损，不透明像素为完好色点。
- 重复单元每边 **4～12 格**；残片像素 `(x,y)` 投影到纹版格 `(x%W, y%H)`。
- 纹版按环形（toroidal）处理：以每格为左上角的 2×2 图块都必须在参考图
  出现过，**跨边、跨角环绕也算**。
- 目标：在所有可行纹版中，最大化各 2×2 图块在参考图中出现次数（频次）之和。
- 求解限时 **5 秒**（墙钟，含安全余量）。

## 求解状态

| 状态 | 含义 |
|------|------|
| `optimal` | 已证最优（达到理论上界，或 CP-SAT 精确证明） |
| `feasible` | 仅找到可行纹样（未证全局最优） |
| `infeasible` | 已证无解 |
| `unknown` | 待定（限时内连可行性都未判定） |

只有 `optimal` / `feasible` 这类有候选的结果会写入 SQLite 并返回 `draft_id`。

## API

### `POST /api/drafts`
multipart 表单：`reference`（PNG）、`fragment`（PNG）、`unit_w`、`unit_h`。

- 同格异色（周期对齐后）→ `409`，返回冲突格与残片**两处原坐标**，不建草案。
- 正常 → 返回状态、图块频次统计、候选网格；有候选时返回 `draft_id`。

### `POST /api/drafts/{id}/points`
在缺损处补录完好色点。表单：`color=r,g,b`，坐标二选一：
- 残片原坐标 `x,y`（自动取模投影）；
- 纹版格坐标 `cell_x,cell_y`。

补录后重新限时求解并更新草案。同格异色 → `409` 且**草案原样保留**。

### `GET /api/drafts/{id}`
查看草案当前状态、全部色点约束、候选网格与图块使用情况。

### `GET /api/drafts/{id}/pattern.png?scale=1`
领取正式纹版 PNG。仅当草案**已证最优**且纹版**符合当前全部色点**时放行，
否则 `409`。

## 裁样 API

裁样单凭**草案编号**开单，开单时把当时可领取的正式纹版（`grid` 快照）与调色板
固化进裁样单。之后草案即使补录色点、重新求解，已开的单仍用开单时的纹版。

### 坐标与记号

- 纹版按 `W × H` 无限平铺，格子坐标可为任意整数（含负数），
  颜色为 `grid[y % H][x % W]`。
- **花心**是平铺后的格坐标 `(x, y)`，提交时需要它是非底色格。
- **刀位**是格线坐标：`x = v` 分隔格 `v-1` 与格 `v`；`y = v` 分隔格 `v-1`
  与格 `v`。裁片矩形的格线为 `left < right`、`top < bottom`，包含的格是
  `left ≤ x < right`、`top ≤ y < bottom`。
- 花朵 = 与花心 4 邻接相连的非底色格连通块，**跨单元计算**。
  若连通块在平铺平面上无限延伸（例如整条竖条纹），返回 `plan_status=infinite`
  并在 `notice` 单独提示，不给出矩形。

### `POST /api/cut-orders`
JSON：`draft_id`、`background`（`[r,g,b]` 或 `"r,g,b"`，须在调色板中）、
`center: {x, y}`、`candidates: {top|bottom|left|right: [格线坐标…]}`。

- 草案必须已证最优且纹版与当前色点一致（否则 `409`）。
- 每边至多 **12** 个候选刀位，刀位距花心格至多 **32** 格、且必须在花心
  对应的一侧；四条边都必须给出（可为空数组）。
- 在所有“包住整朵花”的候选组合里选**面积最小**的矩形；并列时按
  `(top,left,bottom,right)` 取最小，保证结果确定。
- 邻花约束：矩形任一边两侧相邻的两个格不能同时为非底色。邻花**整朵**落在
  裁片内是允许的，只禁止刀线从一朵花中间穿过。
- 成功返回 `201` 与单号 `order_no`（形如 `CO-000001`）。无可行方案时仍开单，
  返回 `plan_status=no_plan` 与 `plan_reason`。

### `POST /api/cut-orders/{order_id}/cuts`
JSON：`edge`（`top|bottom|left|right`），可选 `line`（须等于当前方案在该边的
刀线）。登记一条**已按方案下刀**的边并重算。重复登记同一边、刀线与方案不符
均返回 `409`。

### `PATCH /api/cut-orders/{order_id}/candidates`
JSON：`candidates: {边: [刀位…]}`（可只给部分边，给出即整条替换）。
修改候选刀位后重算。

- **已切位置永远参与重算**：即使候选刀位被改掉，已登记的边仍用登记时的刀线；
  已下刀的边不允许再改其候选刀位（`409 edge_already_cut`）。
- **无解时撤下未执行刀线**：重算无解时，上一版方案里未下刀的边记入
  `withdrawn`（仅提示），已切位置保留，供下次重算继续使用。
- 重启或查询（`GET`）沿用已落库的方案，不会因为查询而改写状态。

### `GET /api/cut-orders/{order_id}`
返回单号、草案编号、开单时纹版快照、底色、花心、四边候选、已切位置、当前方案
与 `withdrawn`。

### 裁样最小示例

```bash
# 1) 开单：花心 (2,2)，白底，四边候选刀位
curl -sS -X POST http://127.0.0.1:8000/api/cut-orders \
  -H 'Content-Type: application/json' \
  -d '{"draft_id":3,"background":[245,245,245],
       "center":{"x":2,"y":2},
       "candidates":{"top":[0,1,2],"bottom":[4,5,6],
                     "left":[0,1,2],"right":[4,5,6]}}'

# 2) 登记上边已下刀
curl -sS -X POST http://127.0.0.1:8000/api/cut-orders/1/cuts \
  -H 'Content-Type: application/json' -d '{"edge":"top"}'

# 3) 改底部候选刀位（已切的上边仍用 y=2 重算）
curl -sS -X PATCH http://127.0.0.1:8000/api/cut-orders/1/candidates \
  -H 'Content-Type: application/json' -d '{"candidates":{"bottom":[5,6]}}'
```

完整可运行流程见 `bash demo/demo_requests.sh`（第 8～13 步）。

## 求解器设计（三层）

1. **CP-SAT 精确优化（短预算）**：结构简单/约束多时直接证最优。
2. **CP-SAT 纯可行性**：颜色整数变量 + 每位置一条 `add_allowed_assignments`
   四元组允许表（≤256 行），最坏尺寸通常 0.1 秒内判定可行性或严格证无解。
3. **局部搜索**：在可行网格上单格变色爬山 + 随机重启，增量评估只看受影响的
   4 个图块，用满剩余预算冲最高分；达到「格数 × 最高频次」理论上界即声明最优。

无 OR-Tools 时自动退回内置的行编码回溯 + 分支定界（带递归剪枝与时限检查）。

## 测试

```bash
pip install pytest httpx
python -m pytest tests/ -v
```

`tests/test_app.py` 覆盖纹版修复，`tests/test_cutting.py` 覆盖裁样的花朵追踪
（含跨单元、无限延伸）、刀位校验、最小面积矩形、邻花约束、开单/登记/改刀位、
重启保持已切位置、无解撤线与纹版快照隔离。
