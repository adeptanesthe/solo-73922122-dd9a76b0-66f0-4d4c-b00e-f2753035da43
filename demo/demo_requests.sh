#!/usr/bin/env bash
# 提花织带纹版修复 API 演示请求。
#
# 前置：
#   1. docker compose up --build -d   （或本地：uvicorn app.main:app --port 8000）
#   2. python demo/make_demo_data.py  （仓库已附带生成好的 PNG，可跳过）
#
# 用法：bash demo/demo_requests.sh [BASE_URL]
set -euo pipefail

BASE_URL="${1:-http://127.0.0.1:8000}"
DEMO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT_DIR="${DEMO_DIR}/out"
mkdir -p "${OUT_DIR}"

echo "== 0. 健康检查 =="
curl -sS "${BASE_URL}/health"
echo

echo "== 1. 竖条纹残片（6×6 重复单元，期望已证最优）=="
create_resp=$(curl -sS -X POST "${BASE_URL}/api/drafts" \
    -F "reference=@${DEMO_DIR}/reference_stripes.png;type=image/png" \
    -F "fragment=@${DEMO_DIR}/fragment_stripes.png;type=image/png" \
    -F "unit_w=6" -F "unit_h=6")
echo "${create_resp}" | python3 -m json.tool
draft_id=$(echo "${create_resp}" | python3 -c "import sys,json; print(json.load(sys.stdin).get('draft_id',''))")
test -n "${draft_id}" || { echo "未获得草案编号，终止"; exit 1; }

echo "== 2. 查看草案 ${draft_id} =="
curl -sS "${BASE_URL}/api/drafts/${draft_id}" | python3 -m json.tool

echo "== 3. 在缺损处补录一个完好色点（格坐标 (4,4)=红）=="
curl -sS -X POST "${BASE_URL}/api/drafts/${draft_id}/points" \
    -F "cell_x=4" -F "cell_y=4" -F "color=220,60,60" | python3 -m json.tool

echo "== 4. 同格异色补录（期望 409 冲突，草案保留）=="
curl -sS -o "${OUT_DIR}/conflict.json" -w "HTTP %{http_code}\n" \
    -X POST "${BASE_URL}/api/drafts/${draft_id}/points" \
    -F "cell_x=4" -F "cell_y=4" -F "color=60,90,220"
cat "${OUT_DIR}/conflict.json" | python3 -m json.tool

echo "== 5. 领取正式纹版 PNG（已证最优才放行）=="
curl -sS -o "${OUT_DIR}/pattern_${draft_id}.png" -w "HTTP %{http_code}\n" \
    "${BASE_URL}/api/drafts/${draft_id}/pattern.png?scale=8"
file "${OUT_DIR}/pattern_${draft_id}.png" || true

echo "== 6. 初始残片同格异色（期望 409，返回两处原坐标）=="
curl -sS -o "${OUT_DIR}/initial_conflict.json" -w "HTTP %{http_code}\n" \
    -X POST "${BASE_URL}/api/drafts" \
    -F "reference=@${DEMO_DIR}/reference_conflict.png;type=image/png" \
    -F "fragment=@${DEMO_DIR}/fragment_conflict.png;type=image/png" \
    -F "unit_w=4" -F "unit_h=4"
cat "${OUT_DIR}/initial_conflict.json" | python3 -m json.tool

echo "== 7. 棋盘格残片（4×4 单元）=="
curl -sS -X POST "${BASE_URL}/api/drafts" \
    -F "reference=@${DEMO_DIR}/reference_checker.png;type=image/png" \
    -F "fragment=@${DEMO_DIR}/fragment_checker.png;type=image/png" \
    -F "unit_w=4" -F "unit_h=4" | python3 -m json.tool

# ---------------------------------------------------------------------------
# 裁样单演示：白底两朵花，开单 -> 登记已切 -> 改刀位重算
# ---------------------------------------------------------------------------
echo "== 8. 为裁样演示开草案（8×8 白底两朵花，期望已证最优）=="
cut_draft_resp=$(curl -sS -X POST "${BASE_URL}/api/drafts" \
    -F "reference=@${DEMO_DIR}/reference_cut.png;type=image/png" \
    -F "fragment=@${DEMO_DIR}/fragment_cut.png;type=image/png" \
    -F "unit_w=8" -F "unit_h=8")
echo "${cut_draft_resp}" | python3 -c \
    "import sys,json; d=json.load(sys.stdin); print(json.dumps({k:d[k] for k in ('status','draft_id','score')}, ensure_ascii=False, indent=2))"
cut_draft_id=$(echo "${cut_draft_resp}" | python3 -c "import sys,json; print(json.load(sys.stdin).get('draft_id',''))")
test -n "${cut_draft_id}" || { echo "未获得裁样草案编号，终止"; exit 1; }

echo "== 9. 开裁样单（花心 (2,2)，底色 245,245,245）=="
cut_order_resp=$(curl -sS -X POST "${BASE_URL}/api/cut-orders" \
    -H "Content-Type: application/json" \
    -d "{
          \"draft_id\": ${cut_draft_id},
          \"background\": [245, 245, 245],
          \"center\": {\"x\": 2, \"y\": 2},
          \"candidates\": {
            \"top\": [0, 1, 2],
            \"bottom\": [4, 5, 6],
            \"left\": [0, 1, 2],
            \"right\": [4, 5, 6]
          }
        }")
echo "${cut_order_resp}" | python3 -c \
    "import sys,json; d=json.load(sys.stdin); print(json.dumps({'order_no':d['order_no'],'plan_status':d['plan_status'],'grid_lines':d['plan']['grid_lines'],'area':d['plan']['area']}, ensure_ascii=False, indent=2))"
cut_order_id=$(echo "${cut_order_resp}" | python3 -c "import sys,json; print(json.load(sys.stdin).get('order_id',''))")
test -n "${cut_order_id}" || { echo "未获得裁样单号，终止"; exit 1; }

echo "== 10. 登记上边已经按方案下刀（刀线 y=2）=="
curl -sS -X POST "${BASE_URL}/api/cut-orders/${cut_order_id}/cuts" \
    -H "Content-Type: application/json" \
    -d '{"edge": "top"}' | python3 -c \
    "import sys,json; d=json.load(sys.stdin); print(json.dumps({'order_no':d['order_no'],'cuts':d['cuts'],'grid_lines':d['plan']['grid_lines']}, ensure_ascii=False, indent=2))"

echo "== 11. 改底部候选刀位并重算（已切的上边仍用 y=2）=="
curl -sS -X PATCH "${BASE_URL}/api/cut-orders/${cut_order_id}/candidates" \
    -H "Content-Type: application/json" \
    -d '{"candidates": {"bottom": [5, 6]}}' | python3 -c \
    "import sys,json; d=json.load(sys.stdin); print(json.dumps({'cuts':d['cuts'],'grid_lines':d['plan']['grid_lines'],'withdrawn':d['withdrawn']}, ensure_ascii=False, indent=2))"

echo "== 12. 把底部候选改到花朵内部，制造无解（未执行刀线撤下，上边保留）=="
curl -sS -X PATCH "${BASE_URL}/api/cut-orders/${cut_order_id}/candidates" \
    -H "Content-Type: application/json" \
    -d '{"candidates": {"bottom": [3]}}' | python3 -c \
    "import sys,json; d=json.load(sys.stdin); print(json.dumps({'plan_status':d['plan_status'],'plan_reason':d['plan_reason'],'cuts':d['cuts'],'withdrawn':d['withdrawn']}, ensure_ascii=False, indent=2))"

echo "== 13. 裁样单详情（重启/查询都沿用已落库方案）=="
curl -sS "${BASE_URL}/api/cut-orders/${cut_order_id}" | python3 -c \
    "import sys,json; d=json.load(sys.stdin); print(json.dumps({'order_no':d['order_no'],'draft_id':d['draft_id'],'plan_status':d['plan_status'],'cuts':d['cuts'],'withdrawn':d['withdrawn']}, ensure_ascii=False, indent=2))"

echo
echo "演示完成，输出目录：${OUT_DIR}"
