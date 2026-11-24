"""裁样 API 的单元与端到端测试。

运行：``PYTHONPATH=/tmp/pylibs:/workspace python3 -m pytest tests/ -v``
"""

from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import cutting, storage
from app.main import app

WHITE = (240, 240, 240)
RED = (220, 60, 60)
BLUE = (60, 90, 220)


# --------------------------------------------------------------------------- #
# 构造工具
# --------------------------------------------------------------------------- #

def png_bytes(grid, transparent_pred=None) -> bytes:
    height = len(grid)
    width = len(grid[0])
    img = Image.new("RGBA", (width, height))
    for y, row in enumerate(grid):
        for x, color in enumerate(row):
            if transparent_pred and transparent_pred(x, y, color):
                img.putpixel((x, y), (0, 0, 0, 0))
            else:
                img.putpixel((x, y), (*color, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def flower_grid(size=8, blocks=((2, 2),)):
    """白底 + 每个坐标各一块 2×2 红花。"""
    grid = [[WHITE] * size for _ in range(size)]
    for bx, by in blocks:
        for y in range(by, by + 2):
            for x in range(bx, bx + 2):
                grid[y % size][x % size] = RED
    return grid


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db = tmp_path / "cut_test.db"
    monkeypatch.setattr(storage, "DEFAULT_DB_PATH", db)
    with TestClient(app) as c:
        yield c


def _make_draft(client, grid, keep_cells=None, size=8):
    """开一个草案；残片只保留 ``keep_cells``（默认只保留红花格）。"""
    keep = keep_cells if keep_cells is not None else {
        (x, y) for y in range(size) for x in range(size)
        if grid[y][x] != WHITE
    }
    resp = client.post(
        "/api/drafts",
        files={
            "reference": ("ref.png", png_bytes(grid), "image/png"),
            "fragment": (
                "frag.png",
                png_bytes(grid, transparent_pred=lambda x, y, c: (x, y) not in keep),
                "image/png",
            ),
        },
        data={"unit_w": size, "unit_h": size},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "optimal", body["status"]
    return body["draft_id"], body["candidate_grid"]


def _order_body(draft_id, *, center=(2, 2), candidates=None, background=WHITE):
    return {
        "draft_id": draft_id,
        "background": list(background),
        "center": {"x": center[0], "y": center[1]},
        "candidates": candidates or {
            "top": [0, 1, 2],
            "bottom": [4, 5, 6],
            "left": [0, 1, 2],
            "right": [4, 5, 6],
        },
    }


# --------------------------------------------------------------------------- #
# cutting 单元测试
# --------------------------------------------------------------------------- #

def test_trace_single_flower_bounds():
    grid = tuple(tuple(row) for row in flower_grid(8))
    trace = cutting.trace_flower(grid, WHITE, (2, 2))
    assert not trace.infinite
    assert trace.cell_count == 4
    assert trace.bounds == {"x_min": 2, "x_max": 3, "y_min": 2, "y_max": 3}


def test_trace_crosses_unit_boundary():
    """花在 (7,7)，平铺后与下方 (7,0)→(7,8) 的同一朵相连。"""
    grid = [[WHITE] * 8 for _ in range(8)]
    for x, y in ((7, 7), (7, 0)):
        grid[y][x] = RED
    trace = cutting.trace_flower(tuple(tuple(r) for r in grid), WHITE, (7, 7))
    assert not trace.infinite
    assert trace.cell_count == 2
    # 在无限平面上这是 (7,7) 与 (7,8)，即跨单元边界
    assert trace.bounds == {"x_min": 7, "x_max": 7, "y_min": 7, "y_max": 8}


def test_trace_detects_infinite_stripe():
    grid = tuple(
        tuple(RED if x % 2 == 0 else BLUE for x in range(8))
        for _ in range(8)
    )
    trace = cutting.trace_flower(grid, RED, (1, 0))
    assert trace.infinite
    assert trace.infinite_residue is not None


def test_trace_detects_infinite_blob():
    """一整片非底色也会无限延伸。"""
    grid = tuple(tuple(RED for _ in range(4)) for _ in range(4))
    assert cutting.trace_flower(grid, WHITE, (0, 0)).infinite


def test_trace_center_on_background():
    grid = tuple(tuple(row) for row in flower_grid(8))
    trace = cutting.trace_flower(grid, WHITE, (0, 0))
    assert trace.center_is_background
    assert not trace.infinite


def test_center_on_background_makes_plan_impossible():
    grid = tuple(tuple(row) for row in flower_grid(8))
    trace = cutting.trace_flower(grid, WHITE, (0, 0))
    result = cutting.select_rectangle(
        grid, WHITE, trace,
        {"top": [0], "bottom": [1], "left": [0], "right": [1]}, {})
    assert result.rectangle is None
    assert result.reason == "center_is_background"


def test_candidate_validation_limits():
    center = (2, 2)
    with pytest.raises(cutting.CutValidationError):
        cutting.validate_edge_lines("top", list(range(13)), center)
    # 重复值去重后不算超限
    assert cutting.validate_edge_lines("top", [2, 2, 2], center) == [2]
    # 距花心超过 32 格
    with pytest.raises(cutting.CutValidationError):
        cutting.validate_edge_lines("top", [2 - 33], center)
    # 错误方向
    with pytest.raises(cutting.CutValidationError):
        cutting.validate_edge_lines("left", [3], center)
    with pytest.raises(cutting.CutValidationError):
        cutting.validate_edge_lines("middle", [0], center)
    # 12 个合法去重后通过
    assert cutting.validate_edge_lines(
        "top", [2 - i for i in range(12)], center) == sorted(
            [2 - i for i in range(12)])
    # 13 个唯一值超限
    with pytest.raises(cutting.CutValidationError):
        cutting.validate_edge_lines("top", [2 - i for i in range(13)], center)


def test_candidate_validation_requires_all_edges():
    with pytest.raises(cutting.CutValidationError):
        cutting.validate_candidate_map({"top": [0]}, (2, 2), require_all=True)
    with pytest.raises(cutting.CutValidationError):
        cutting.validate_candidate_map(
            {"top": [0], "bottom": [1], "left": [0], "right": [1], "other": []},
            (2, 2), require_all=True)
    ok = cutting.validate_candidate_map(
        {"top": [0], "bottom": [4], "left": [0], "right": [4]},
        (2, 2), require_all=True)
    assert ok == {"top": [0], "bottom": [4], "left": [0], "right": [4]}


def test_min_area_rectangle_picked():
    grid = tuple(tuple(row) for row in flower_grid(8))
    trace = cutting.trace_flower(grid, WHITE, (2, 2))
    candidates = {
        "top": [0, 2], "bottom": [4, 6], "left": [0, 2], "right": [4, 6],
    }
    result = cutting.select_rectangle(grid, WHITE, trace, candidates, {})
    assert result.rectangle == cutting.Rectangle(left=2, right=4, top=2, bottom=4)
    assert result.rectangle.area == 4


def test_rectangle_must_include_whole_flower():
    grid = tuple(tuple(row) for row in flower_grid(8))
    trace = cutting.trace_flower(grid, WHITE, (3, 3))
    # 左/上刀位都落在花朵中间 —— 没有任何组合包住花
    result = cutting.select_rectangle(
        grid, WHITE, trace,
        {"top": [3], "bottom": [4], "left": [3], "right": [4]}, {})
    assert result.rectangle is None
    assert result.reason.startswith("no_line_contains_flower")


def test_boundary_conflict_rejects_cut_through_neighbor():
    """刀线从邻花中间穿过（花被拦腰切断）-> 违反邻花约束，应换更大刀位。"""
    grid = [[WHITE] * 8 for _ in range(8)]
    grid[2][2] = RED              # 目标花：单格
    grid[2][4] = BLUE             # 邻花：横向两格 (4,2)-(5,2)
    grid[2][5] = BLUE
    grid = tuple(tuple(r) for r in grid)
    trace = cutting.trace_flower(grid, WHITE, (2, 2))
    assert trace.cell_count == 1
    # right=5 时外侧 (5,2) 与内侧 (4,2) 都是非底色 -> 把邻花切开
    # right=6 才能完整包住邻花，边界外是底色
    result = cutting.select_rectangle(
        grid, WHITE, trace,
        {"top": [2], "bottom": [3], "left": [2], "right": [5, 6]}, {})
    assert result.rectangle == cutting.Rectangle(left=2, right=6, top=2, bottom=3)


def test_connected_flower_uses_bounding_box():
    """与花心相连的整片花纹都算一朵，刀线必须包住其包围盒。"""
    grid = [[WHITE] * 8 for _ in range(8)]
    for y in range(2, 4):
        for x in range(2, 6):
            grid[y][x] = RED          # 一条 4×2 连通花纹
    grid = tuple(tuple(r) for r in grid)
    trace = cutting.trace_flower(grid, WHITE, (2, 2))
    assert not trace.infinite  # 4×2 连通块在 8×8 周期内仍有界
    result = cutting.select_rectangle(
        grid, WHITE, trace,
        {"top": [2], "bottom": [4], "left": [2], "right": [3, 4, 5, 6]}, {})
    # 只有 right=6 能包住整朵花
    assert result.rectangle == cutting.Rectangle(left=2, right=6, top=2, bottom=4)


def test_trace_handles_negative_coordinates():
    """平铺平面允许负数坐标；花心 (-4,-4) 等价于纹版格 (0,0)。"""
    grid = [[WHITE] * 4 for _ in range(4)]
    grid[0][0] = RED
    grid = tuple(tuple(r) for r in grid)
    trace = cutting.trace_flower(grid, WHITE, (-4, -4))
    assert not trace.infinite
    assert trace.cell_count == 1
    result = cutting.select_rectangle(
        grid, WHITE, trace,
        {"top": [-4], "bottom": [-3], "left": [-4], "right": [-3]}, {})
    assert result.rectangle == cutting.Rectangle(
        left=-4, right=-3, top=-4, bottom=-3)


def test_separate_neighbor_flower_can_be_inside_piece():
    """邻花整朵落在裁片内是允许的，只禁止刀线从花中间穿过。"""
    grid = [[WHITE] * 8 for _ in range(8)]
    for bx, by in ((2, 2), (4, 2)):
        for y in range(by, by + 2):
            for x in range(bx, bx + 2):
                grid[y][x] = RED
    grid = tuple(tuple(r) for r in grid)
    trace = cutting.trace_flower(grid, WHITE, (2, 2))
    result = cutting.select_rectangle(
        grid, WHITE, trace,
        {"top": [2], "bottom": [4], "left": [2], "right": [6]}, {})
    assert result.rectangle == cutting.Rectangle(left=2, right=6, top=2, bottom=4)


def test_cut_edge_is_sticky_even_if_not_candidate():
    grid = tuple(tuple(row) for row in flower_grid(8))
    trace = cutting.trace_flower(grid, WHITE, (2, 2))
    result = cutting.select_rectangle(
        grid, WHITE, trace,
        {"top": [0], "bottom": [4, 5], "left": [0], "right": [4, 5]},
        {"top": 1})  # 已切位置 1 不在候选里
    assert result.rectangle is not None
    assert result.rectangle.top == 1


# --------------------------------------------------------------------------- #
# 端到端：开单 / 登记 / 改刀位 / 快照
# --------------------------------------------------------------------------- #

def test_create_cut_order_returns_number_and_rectangle(client):
    draft_id, _ = _make_draft(client, flower_grid(8))
    resp = client.post("/api/cut-orders", json=_order_body(draft_id))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["order_no"] == f"CO-{body['order_id']:06d}"
    assert body["draft_id"] == draft_id
    assert body["plan_status"] == "planned"
    assert body["plan"]["grid_lines"] == {
        "left": 2, "right": 4, "top": 2, "bottom": 4}
    assert body["plan"]["area"] == 4
    assert body["plan"]["flower"]["cell_count"] == 4
    assert body["cuts"] == {}
    assert body["plan"]["cut_edges"] == []
    # 快照固定开单时的纹版
    assert body["pattern_snapshot"]["grid"][2][2] == list(RED)


def test_create_requires_optimal_draft(client):
    grid = flower_grid(8)
    resp = client.post(
        "/api/drafts",
        files={
            "reference": ("ref.png", png_bytes(grid), "image/png"),
            "fragment": ("frag.png", png_bytes(grid, transparent_pred=lambda x, y, c: True),
                         "image/png"),
        },
        data={"unit_w": 8, "unit_h": 8},
    )
    draft_id = resp.json()["draft_id"]
    from app.main import _store
    with _store._lock:
        _store._conn.execute(
            "UPDATE drafts SET status='feasible' WHERE id=?", (draft_id,))
        _store._conn.commit()
    blocked = client.post("/api/cut-orders", json=_order_body(draft_id))
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["error"] == "not_optimal"


def test_create_rejects_bad_background_and_center(client):
    draft_id, _ = _make_draft(client, flower_grid(8))
    bad_bg = client.post("/api/cut-orders",
                         json=_order_body(draft_id, background=(1, 2, 3)))
    assert bad_bg.status_code == 400
    bad_center = _order_body(draft_id)
    bad_center["center"] = {"x": "a", "y": 2}
    assert client.post("/api/cut-orders", json=bad_center).status_code == 422
    missing_edge = _order_body(draft_id)
    del missing_edge["candidates"]["right"]
    assert client.post("/api/cut-orders", json=missing_edge).status_code == 400
    too_many = _order_body(draft_id)
    too_many["candidates"]["right"] = list(range(13))  # 超出 12 个
    assert client.post("/api/cut-orders", json=too_many).status_code == 400
    too_far = _order_body(draft_id)
    too_far["candidates"]["top"] = [2 - 33]  # 超过 32 格
    assert client.post("/api/cut-orders", json=too_far).status_code == 400


def test_background_accepts_rgb_string(client):
    draft_id, _ = _make_draft(client, flower_grid(8))
    body = _order_body(draft_id)
    body["background"] = "240,240,240"
    resp = client.post("/api/cut-orders", json=body)
    assert resp.status_code == 201, resp.text
    assert resp.json()["background"] == list(WHITE)


def test_create_order_unknown_draft_404(client):
    assert client.post("/api/cut-orders",
                       json=_order_body(9999)).status_code == 404


def test_register_cut_and_recompute_uses_cut_line(client):
    draft_id, _ = _make_draft(client, flower_grid(8))
    order = client.post("/api/cut-orders", json=_order_body(draft_id)).json()
    order_id = order["order_id"]

    reg = client.post(f"/api/cut-orders/{order_id}/cuts",
                      json={"edge": "top"})
    assert reg.status_code == 200, reg.text
    body = reg.json()
    assert body["cuts"] == {"top": 2}
    assert body["registered"] == {"edge": "top", "line": 2, "cut_count": 1}

    # 再登记一次同一边 -> 409
    again = client.post(f"/api/cut-orders/{order_id}/cuts",
                        json={"edge": "top"})
    assert again.status_code == 409
    assert again.json()["detail"]["error"] == "edge_already_cut"


def test_register_cut_line_must_match_plan(client):
    draft_id, _ = _make_draft(client, flower_grid(8))
    order_id = client.post(
        "/api/cut-orders", json=_order_body(draft_id)).json()["order_id"]
    bad = client.post(f"/api/cut-orders/{order_id}/cuts",
                      json={"edge": "top", "line": 0})
    assert bad.status_code == 409
    assert bad.json()["detail"]["error"] == "line_mismatch"


def test_patch_candidates_keeps_executed_cut(client):
    draft_id, _ = _make_draft(client, flower_grid(8))
    order_id = client.post(
        "/api/cut-orders", json=_order_body(draft_id)).json()["order_id"]
    client.post(f"/api/cut-orders/{order_id}/cuts", json={"edge": "top"})

    # 已切边的候选刀位不可改
    bad = client.patch(
        f"/api/cut-orders/{order_id}/candidates",
        json={"candidates": {"top": [1]}})
    assert bad.status_code == 409
    assert bad.json()["detail"]["error"] == "edge_already_cut"

    # 改其他边：重算后 top 仍用已切位置 2
    patched = client.patch(
        f"/api/cut-orders/{order_id}/candidates",
        json={"candidates": {"bottom": [5, 6]}})
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["cuts"] == {"top": 2}
    assert body["plan"]["grid_lines"]["top"] == 2
    assert body["plan"]["grid_lines"]["bottom"] == 5


def test_patch_executed_edge_candidates_rejected(client):
    """已切边的候选刀位不允许再改。"""
    draft_id, _ = _make_draft(client, flower_grid(8))
    order_id = client.post(
        "/api/cut-orders", json=_order_body(draft_id)).json()["order_id"]
    client.post(f"/api/cut-orders/{order_id}/cuts", json={"edge": "left"})
    patched = client.patch(
        f"/api/cut-orders/{order_id}/candidates",
        json={"candidates": {"left": []}})
    assert patched.status_code == 409
    assert patched.json()["detail"]["error"] == "edge_already_cut"
    # 已切位置不变
    got = client.get(f"/api/cut-orders/{order_id}").json()
    assert got["cuts"] == {"left": 2}


def test_no_solution_withdraws_unexecuted_lines(client):
    draft_id, _ = _make_draft(client, flower_grid(8))
    order_id = client.post(
        "/api/cut-orders", json=_order_body(draft_id)).json()["order_id"]
    client.post(f"/api/cut-orders/{order_id}/cuts", json={"edge": "top"})

    # bottom=3 在花朵内部 -> 没有可行方案
    patched = client.patch(
        f"/api/cut-orders/{order_id}/candidates",
        json={"candidates": {"bottom": [3]}})
    body = patched.json()
    assert body["plan_status"] == "no_plan"
    assert body["plan"]["available"] is False
    assert body["plan_reason"].startswith("no_line_contains_flower")
    # 未执行的刀线被撤下，已切位置保留
    assert body["withdrawn"] == {"left": 2, "right": 4, "bottom": 4}
    assert body["cuts"] == {"top": 2}

    # 恢复候选后，重算仍使用已切位置
    restored = client.patch(
        f"/api/cut-orders/{order_id}/candidates",
        json={"candidates": {"bottom": [4, 5, 6]}}).json()
    assert restored["plan_status"] == "planned"
    assert restored["withdrawn"] == {}
    assert restored["cuts"] == {"top": 2}
    assert restored["plan"]["grid_lines"] == {
        "left": 2, "right": 4, "top": 2, "bottom": 4}


def test_sticky_cut_line_that_excludes_flower_is_reported(client):
    """已切刀线若包不住花（例如花朵配置变了），给出专门的无解原因。"""
    grid = tuple(tuple(row) for row in flower_grid(8))
    trace = cutting.trace_flower(grid, WHITE, (2, 2))
    result = cutting.select_rectangle(
        grid, WHITE, trace,
        {"top": [0], "bottom": [4], "left": [0], "right": [4]},
        {"top": 3})  # 已切 top=3 落在花朵内部的 y=3 格，包不住花
    assert result.rectangle is None
    assert result.reason == "cut_line_excludes_flower:top"


def test_restart_keeps_cut_positions(client, tmp_path):
    """重新打开应用（模拟重启）后，已切位置和方案仍在，可用于重算。"""
    draft_id, _ = _make_draft(client, flower_grid(8))
    order_id = client.post(
        "/api/cut-orders", json=_order_body(draft_id)).json()["order_id"]
    client.post(f"/api/cut-orders/{order_id}/cuts", json={"edge": "top"})
    client.post(f"/api/cut-orders/{order_id}/cuts", json={"edge": "right"})

    # 新进程的 store 重新读取同一个数据库
    with TestClient(app) as restarted:
        got = restarted.get(f"/api/cut-orders/{order_id}")
        assert got.status_code == 200
        body = got.json()
        assert body["cuts"] == {"top": 2, "right": 4}
        assert body["plan"]["grid_lines"] == {
            "left": 2, "right": 4, "top": 2, "bottom": 4}
        assert body["draft_id"] == draft_id

        # 重启后改另一条边的候选刀位：仍用已切位置重算
        patched = restarted.patch(
            f"/api/cut-orders/{order_id}/candidates",
            json={"candidates": {"bottom": [5, 6]}})
        assert patched.status_code == 200
        assert patched.json()["cuts"] == {"top": 2, "right": 4}
        assert patched.json()["plan"]["grid_lines"] == {
            "left": 2, "right": 4, "top": 2, "bottom": 5}


def test_infinite_flower_reports_notice(client):
    stripe = [[RED if x % 2 == 0 else BLUE for x in range(8)]
              for _ in range(8)]
    draft_id, _ = _make_draft(client, stripe)
    resp = client.post("/api/cut-orders", json={
        "draft_id": draft_id,
        "background": list(RED),
        "center": {"x": 1, "y": 0},
        "candidates": {
            "top": [0], "bottom": [1, 2], "left": [0, 1], "right": [2, 3],
        },
    })
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["plan_status"] == "infinite"
    assert body["plan"]["available"] is False
    assert body["plan_reason"] == "flower_infinite"
    assert body["plan"]["flower"]["infinite"] is True
    assert "无限延伸" in body["notice"]


def test_flower_crossing_unit_boundary_is_one_flower(client):
    """花在单元底部与顶部相连，裁样按跨单元追踪。"""
    grid = [[WHITE] * 8 for _ in range(8)]
    for x, y in ((7, 7), (7, 0)):
        grid[y][x] = RED
    draft_id, _ = _make_draft(client, grid)
    resp = client.post("/api/cut-orders", json={
        "draft_id": draft_id,
        "background": list(WHITE),
        "center": {"x": 7, "y": 7},
        "candidates": {
            "top": [7], "bottom": [9], "left": [7], "right": [8],
        },
    })
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["plan_status"] == "planned"
    # (7,7) 与跨单元的 (7,8) 相连，最小矩形为 y=7..9、x=7..8
    assert body["plan"]["grid_lines"] == {
        "left": 7, "right": 8, "top": 7, "bottom": 9}
    assert body["plan"]["flower"]["cell_count"] == 2


def test_draft_point_added_later_does_not_change_snapshot(client):
    """开单后草案补录色点，裁样单仍用开单时的纹版。"""
    draft_id, _ = _make_draft(client, flower_grid(8))
    order = client.post("/api/cut-orders", json=_order_body(draft_id)).json()
    order_id = order["order_id"]
    snapshot_before = order["pattern_snapshot"]["grid"]

    # 补录一个白底色点（合法、不破坏草案最优）
    added = client.post(
        f"/api/drafts/{draft_id}/points",
        data={"cell_x": 0, "cell_y": 0, "color": "240,240,240"})
    assert added.status_code == 200

    got = client.get(f"/api/cut-orders/{order_id}").json()
    assert got["pattern_snapshot"]["grid"] == snapshot_before
    assert got["plan_status"] == "planned"
    assert got["cuts"] == {}


def test_get_unknown_cut_order_404(client):
    assert client.get("/api/cut-orders/9999").status_code == 404
