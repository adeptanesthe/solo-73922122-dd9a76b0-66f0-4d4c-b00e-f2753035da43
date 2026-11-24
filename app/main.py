"""提花织带纹版修复 FastAPI 应用。

端点
----
``POST /api/drafts``
    上传参考 PNG、残片 PNG 与重复单元尺寸；校验周期对齐并限时 5 秒求解，
    有候选时落库 SQLite 并返回草案编号。

``POST /api/drafts/{draft_id}/points``
    在缺损处补录完好色点（残片原坐标 + 颜色，或纹版格坐标 + 颜色），
    重新限时求解；色点冲突时返回坐标并保留原草案。

``GET  /api/drafts/{draft_id}``
    查看草案当前状态、约束与候选网格。

``GET  /api/drafts/{draft_id}/pattern.png``
    领取正式纹版 PNG；仅当草案已证最优且与当前全部色点一致时放行。

``POST /api/cut-orders``
    凭草案编号开裁样单：固定当时可领取的正式纹版，提交底色、花心格坐标与
    四边候选刀位，返回单号与最小面积裁片矩形。

``POST /api/cut-orders/{order_id}/cuts``
    凭单号登记一条已按方案下刀的边，并用已切位置重算。

``PATCH /api/cut-orders/{order_id}/candidates``
    修改一至多条边的候选刀位并重算；已下刀的边仍沿用已切位置。

``GET  /api/cut-orders/{order_id}``
    查看裁样单详情（开单时的纹版快照、候选刀位、已切位置与当前方案）。

``GET  /health``
    健康检查。
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Literal, Optional, Union

from fastapi import FastAPI, File, Form, HTTPException, Path, Query, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from .cutting import (
    EDGE_LABELS,
    EDGES,
    CutValidationError,
    FlowerTrace,
    PlanResult,
    Rectangle,
    rectangle_payload,
    select_rectangle,
    trace_flower,
    validate_candidate_map,
)
from .images import (
    ImageValidationError,
    MAX_UNIT,
    MIN_UNIT,
    RGB,
    TileKey,
    grid_to_png,
    parse_fragment,
    parse_reference,
    project_points,
    validate_unit_size,
)
from .solver import (
    STATUS_FEASIBLE,
    STATUS_INFEASIBLE,
    STATUS_OPTIMAL,
    STATUS_UNKNOWN,
    SolveResult,
    solve,
)
from .storage import DraftStore

SOLVE_TIME_LIMIT = 5.0

_store: Optional[DraftStore] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _store
    from . import storage as storage_module

    _store = DraftStore(storage_module.DEFAULT_DB_PATH)
    yield
    assert _store is not None
    _store.close()


app = FastAPI(
    title="提花织带纹版修复 API",
    version="1.0.0",
    description=(
        "根据同系列参考 PNG 与透明像素标注缺损的残片 PNG，"
        "补出可反复织造的重复单元纹版。"
    ),
    lifespan=lifespan,
)


def store() -> DraftStore:
    assert _store is not None, "DraftStore 尚未初始化"
    return _store


@app.exception_handler(ImageValidationError)
async def _image_validation_handler(_request, exc: ImageValidationError):
    return JSONResponse(status_code=400, content={
        "error": "validation_error",
        "message": str(exc),
    })


@app.exception_handler(CutValidationError)
async def _cut_validation_handler(_request, exc: CutValidationError):
    return JSONResponse(status_code=400, content={
        "error": "validation_error",
        "message": str(exc),
    })


class ConflictError(HTTPException):
    def __init__(self, detail: dict):
        super().__init__(status_code=409, detail=detail)


class CutUnavailableError(HTTPException):
    """当前草案暂时不能开裁样单（如尚未证最优、纹版与色点不一致）。"""

    def __init__(self, detail: dict):
        super().__init__(status_code=409, detail=detail)


# --------------------------------------------------------------------------- #
# 序列化辅助
# --------------------------------------------------------------------------- #

def _tile_counts_payload(tile_counts: dict[TileKey, int]) -> list[dict]:
    return [
        {"tile": [list(c) for c in key], "count": count}
        for key, count in sorted(
            tile_counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


def _tile_counts_from_rows(entries: list[dict]) -> dict[TileKey, int]:
    return {
        tuple(tuple(c) for c in entry["tile"]): entry["count"]  # type: ignore[arg-type]
        for entry in entries
    }


def _conflict_payload(conflicts, unit_w: int, unit_h: int) -> dict:
    return {
        "error": "cell_conflict",
        "message": "完好色点按周期对齐时出现同格异色",
        "unit_size": {"width": unit_w, "height": unit_h},
        "conflicts": [
            {
                "cell": {"x": c.cell[0], "y": c.cell[1]},
                "fragment_positions": [
                    {"x": px, "y": py} for px, py in c.positions
                ],
                "colors": [list(color) for color in c.colors],
            }
            for c in conflicts
        ],
    }


_STATUS_LABELS = {
    STATUS_OPTIMAL: "已证最优",
    STATUS_FEASIBLE: "仅找到可行纹样",
    STATUS_INFEASIBLE: "已证无解",
    STATUS_UNKNOWN: "待定",
}


def _solve_payload(
    result: SolveResult,
    unit_w: int,
    unit_h: int,
    palette: tuple[RGB, ...],
    tile_counts: dict[TileKey, int],
    fixed: dict[tuple[int, int], RGB],
    draft_id: Optional[int],
) -> dict:
    payload = {
        "status": result.status,
        "status_label": _STATUS_LABELS[result.status],
        "unit_size": {"width": unit_w, "height": unit_h},
        "time_limit_seconds": SOLVE_TIME_LIMIT,
        "elapsed_seconds": round(result.elapsed_seconds, 4),
        "solver": result.solver_name,
        "score": result.score,
        "upper_bound": result.upper_bound,
        "fixed_point_count": len(fixed),
        "palette": [list(c) for c in palette],
        "tile_counts": _tile_counts_payload(tile_counts),
        "candidate_grid": (
            [[list(c) for c in row] for row in result.grid]
            if result.grid is not None else None
        ),
        "candidate_tile_usage": result.tile_usage,
    }
    if draft_id is not None:
        payload["draft_id"] = draft_id
        payload["pattern_url"] = f"/api/drafts/{draft_id}/pattern.png"
    return payload


def _parse_rgb(raw: str) -> RGB:
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) != 3:
        raise ImageValidationError("color 需要形如 'r,g,b' 的三色通道")
    try:
        channels = tuple(int(p) for p in parts)
    except ValueError as exc:
        raise ImageValidationError("color 通道必须是 0～255 的整数") from exc
    if any(not 0 <= c <= 255 for c in channels):
        raise ImageValidationError("color 通道必须在 0～255 之间")
    return channels  # type: ignore[return-value]


# --------------------------------------------------------------------------- #
# 裁样单：请求体模型与序列化辅助
# --------------------------------------------------------------------------- #

ColorInput = Union[str, list[int], tuple[int, int, int]]


def _normalize_color(value: ColorInput) -> RGB:
    if isinstance(value, str):
        return _parse_rgb(value)
    if isinstance(value, (list, tuple)) and len(value) == 3:
        channels = tuple(value)
        if all(isinstance(c, int) and not isinstance(c, bool)
               and 0 <= c <= 255 for c in channels):
            return channels  # type: ignore[return-value]
    raise CutValidationError("background 需要 [r,g,b] 或 'r,g,b' 形式的颜色")


class CutOrderCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    draft_id: int = Field(..., ge=1, description="草案编号")
    background: ColorInput = Field(..., description="底色，[r,g,b] 或 'r,g,b'")
    center: dict[str, int] = Field(
        ..., description="平铺后的花心格坐标 {'x': 整数, 'y': 整数}")
    candidates: dict[str, list[int]] = Field(
        ..., description="四边候选刀位（格线坐标）")


class CutRegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    edge: Literal["top", "bottom", "left", "right"] = Field(
        ..., description="要登记的边")
    line: Optional[int] = Field(
        None, description="登记的刀线；省略时取当前方案在该边的刀线")


class CandidatesPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidates: dict[str, list[int]] = Field(
        ..., description="要替换候选刀位的边（可只给部分边）")


PLAN_PLANNED = "planned"
PLAN_INFINITE = "infinite"
PLAN_NO_PLAN = "no_plan"

_PLAN_LABELS = {
    PLAN_PLANNED: "已给出裁片方案",
    PLAN_INFINITE: "花纹无限延伸，无法裁样",
    PLAN_NO_PLAN: "当前无可行方案",
}


def _order_no(order_id: int) -> str:
    return f"CO-{order_id:06d}"


def _trace_payload(trace: FlowerTrace) -> dict:
    return {
        "infinite": trace.infinite,
        "center_is_background": trace.center_is_background,
        "cell_count": trace.cell_count,
        "cell_bounds": (
            {
                "x_min": trace.x_min,
                "x_max": trace.x_max,
                "y_min": trace.y_min,
                "y_max": trace.y_max,
            }
            if trace.bounds is not None else None
        ),
        "infinite_residue": (
            {"x": trace.infinite_residue[0], "y": trace.infinite_residue[1]}
            if trace.infinite_residue else None
        ),
        "infinite_offsets": (
            [{"x": point[0], "y": point[1]}
             for point in trace.infinite_offsets]
            if trace.infinite_offsets else None
        ),
    }


def _plan_payload(result: PlanResult, trace: FlowerTrace,
                  cuts: dict[str, int],
                  candidates: Optional[dict[str, list[int]]] = None) -> dict:
    if result.rectangle is None:
        return {
            "available": False,
            "reason": result.reason,
            "detail": result.detail,
            "flower": _trace_payload(trace),
        }
    rect: Rectangle = result.rectangle
    payload = {
        "available": True,
        "reason": None,
        "detail": None,
        "flower": _trace_payload(trace),
        **rectangle_payload(rect),
        "edge_labels": {
            "top": EDGE_LABELS["top"],
            "bottom": EDGE_LABELS["bottom"],
            "left": EDGE_LABELS["left"],
            "right": EDGE_LABELS["right"],
        },
        "cut_edges": [edge for edge in EDGES if edge in cuts],
        "proposed_edges": [edge for edge in EDGES if edge not in cuts],
    }
    if candidates is not None:
        lines = rect.as_lines()
        payload["edges"] = [
            {
                "edge": edge,
                "label": EDGE_LABELS[edge],
                "line": lines[edge],
                "cut": edge in cuts,
                "candidates": list(candidates.get(edge) or []),
            }
            for edge in EDGES
        ]
    return payload


def _cut_order_payload(row, *, plan_result: PlanResult,
                       trace: FlowerTrace, cuts: dict[str, int],
                       withdrawn: dict[str, int],
                       candidates: dict[str, list[int]]) -> dict:
    plan_status = (
        PLAN_INFINITE if trace.infinite
        else PLAN_PLANNED if plan_result.ok
        else PLAN_NO_PLAN
    )
    payload = {
        "order_id": row["id"],
        "order_no": _order_no(row["id"]),
        "draft_id": row["draft_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "unit_size": {"width": row["unit_w"], "height": row["unit_h"]},
        "background": json.loads(row["background"]),
        "center": {"x": row["center_x"], "y": row["center_y"]},
        "candidates": candidates,
        "cuts": cuts,
        "cut_count": len(cuts),
        "pattern_snapshot": {
            "unit_size": {"width": row["unit_w"], "height": row["unit_h"]},
            "palette": json.loads(row["palette"]),
            "grid": json.loads(row["grid"]),
            "note": "本单固定开单时的纹版快照，之后草案补录色点不影响本单",
        },
        "plan_status": plan_status,
        "plan_status_label": _PLAN_LABELS[plan_status],
        "plan_reason": plan_result.reason,
        "plan_detail": plan_result.detail,
        "plan": _plan_payload(plan_result, trace, cuts, candidates),
        "withdrawn": withdrawn,
    }
    if trace.infinite:
        payload["notice"] = (
            "花纹随平铺无限延伸（同一纹版格在花心中反复出现），"
            "无法裁下一朵完整的花；请调整花心或纹版后重新开单。"
        )
    return payload


def _recompute(
    *,
    grid: tuple[tuple[RGB, ...], ...],
    background: RGB,
    center: tuple[int, int],
    candidates: dict[str, list[int]],
    cuts: dict[str, int],
    previous_plan: Optional[dict],
    previous_withdrawn: Optional[dict[str, int]] = None,
) -> tuple[PlanResult, FlowerTrace, dict[str, int]]:
    """按快照、候选刀位与已切位置重算方案。

    无解时撤下上一版方案里所有未执行的刀线（记入 ``withdrawn``），
    已切位置原样保留，供下次重算继续使用。
    """
    trace = trace_flower(grid, background, center)
    if trace.center_is_background:
        result = PlanResult(None, "center_is_background",
                            "花心格是底色，请改选花纹格")
    elif trace.infinite:
        result = PlanResult(None, "flower_infinite",
                            "花纹随平铺无限延伸，无法裁出一朵完整的花")
    else:
        result = select_rectangle(grid, background, trace, candidates, cuts)

    if result.ok:
        withdrawn: dict[str, int] = {}
    elif previous_plan and previous_plan.get("available"):
        withdrawn = {
            edge: line
            for edge, line in (previous_plan.get("grid_lines") or {}).items()
            if edge not in cuts
        }
    else:
        # 没有新撤下的刀线时保留上一次的撤线记录
        withdrawn = dict(previous_withdrawn or {})
    return result, trace, withdrawn


def _plan_result_from_row(row) -> PlanResult:
    """读取已落库的方案，不做重算（重启/查询保持最后一次结果）。"""
    plan = json.loads(row["plan"]) if row["plan"] else None
    if not plan:
        return PlanResult(None, row["plan_reason"], row["plan_detail"])
    lines = plan["grid_lines"]
    return PlanResult(Rectangle(
        left=lines["left"], right=lines["right"],
        top=lines["top"], bottom=lines["bottom"]))


def _stored_plan(result: PlanResult) -> Optional[dict]:
    """落库用的紧凑方案（只保留坐标与尺寸，附带 available 标记）。"""
    if result.rectangle is None:
        return None
    return {"available": True, **rectangle_payload(result.rectangle)}


def _load_cut_order(order_id: int):
    row = store().get_cut_order(order_id)
    if row is None:
        raise HTTPException(status_code=404, detail={
            "error": "not_found", "message": f"裁样单 {order_id} 不存在"})
    return row


def _stored_cut_state(row) -> tuple[dict[str, list[int]], dict[str, int],
                                    tuple[tuple[RGB, ...], ...], RGB,
                                    tuple[int, int]]:
    candidates = json.loads(row["candidates"])
    cuts = json.loads(row["cuts"])
    grid = DraftStore.decode_grid(row["grid"])
    assert grid is not None
    background = tuple(json.loads(row["background"]))
    center = (row["center_x"], row["center_y"])
    return candidates, cuts, grid, background, center  # type: ignore[return-value]


# --------------------------------------------------------------------------- #
# 端点
# --------------------------------------------------------------------------- #

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/api/drafts")
async def create_draft(
    reference: UploadFile = File(..., description="同系列参考 PNG（≤4 色，不透明）"),
    fragment: UploadFile = File(..., description="残片 PNG，透明像素标缺损"),
    unit_w: int = Form(..., description=f"重复单元宽（格），{MIN_UNIT}～{MAX_UNIT}"),
    unit_h: int = Form(..., description=f"重复单元高（格），{MIN_UNIT}～{MAX_UNIT}"),
):
    validate_unit_size(unit_w, unit_h)
    ref = parse_reference(await reference.read())
    frag = parse_fragment(await fragment.read())

    fixed, conflicts = project_points(frag.points, unit_w, unit_h)
    if conflicts:
        raise ConflictError(_conflict_payload(conflicts, unit_w, unit_h))

    result = solve(ref.colors, ref.tile_counts, unit_w, unit_h, fixed,
                   SOLVE_TIME_LIMIT)

    draft_id: Optional[int] = None
    if result.status in (STATUS_OPTIMAL, STATUS_FEASIBLE):
        draft_id = store().create_draft(
            unit_w=unit_w, unit_h=unit_h, result=result,
            palette=ref.colors, tile_counts=ref.tile_counts,
            fixed_points=fixed,
        )
    return _solve_payload(result, unit_w, unit_h, ref.colors,
                          ref.tile_counts, fixed, draft_id)


@app.post("/api/drafts/{draft_id}/points")
async def add_point(
    draft_id: int = Path(..., ge=1),
    color: str = Form(..., description="完好色点颜色，形如 '255,0,0'"),
    x: Optional[int] = Form(None, description="残片原坐标 x（与 cell_x 二选一）"),
    y: Optional[int] = Form(None, description="残片原坐标 y（与 cell_y 二选一）"),
    cell_x: Optional[int] = Form(None, description="纹版格坐标 x"),
    cell_y: Optional[int] = Form(None, description="纹版格坐标 y"),
):
    row = store().get_draft(draft_id)
    if row is None:
        raise HTTPException(status_code=404, detail={
            "error": "not_found", "message": f"草案 {draft_id} 不存在"})

    rgb = _parse_rgb(color)
    unit_w, unit_h = row["unit_w"], row["unit_h"]

    if cell_x is not None and cell_y is not None:
        target_cell = (cell_x, cell_y)
        source_position = None
    elif x is not None and y is not None:
        target_cell = (x % unit_w, y % unit_h)
        source_position = (x, y)
    else:
        raise ImageValidationError(
            "必须提供残片原坐标 (x,y) 或纹版格坐标 (cell_x,cell_y)")

    cx, cy = target_cell
    if not (0 <= cx < unit_w and 0 <= cy < unit_h):
        raise ImageValidationError(
            f"格坐标超出单元范围：({cx},{cy})，单元 {unit_w}×{unit_h}")

    fixed = DraftStore.decode_points(row["fixed_points"])
    palette = tuple(tuple(c) for c in json.loads(row["palette"]))  # type: ignore[arg-type]
    tile_counts = _tile_counts_from_rows(json.loads(row["tile_counts"]))

    # 同格异色 -> 冲突，草案原样保留
    if target_cell in fixed and fixed[target_cell] != rgb:
        raise ConflictError({
            "error": "cell_conflict",
            "message": "补录色点与该格已有色点颜色冲突，草案保持不变",
            "draft_id": draft_id,
            "conflict": {
                "cell": {"x": cx, "y": cy},
                "existing_color": list(fixed[target_cell]),
                "incoming_color": list(rgb),
                "fragment_position": (
                    {"x": source_position[0], "y": source_position[1]}
                    if source_position else None),
            },
        })

    if rgb not in palette:
        raise ImageValidationError(
            f"颜色 {list(rgb)} 不在参考图调色板 "
            f"{[list(c) for c in palette]} 中")

    fixed[target_cell] = rgb
    result = solve(palette, tile_counts, unit_w, unit_h, fixed,
                   SOLVE_TIME_LIMIT)
    store().update_draft(draft_id, result=result, fixed_points=fixed)

    payload = _solve_payload(result, unit_w, unit_h, palette, tile_counts,
                             fixed, draft_id)
    payload["added"] = {
        "cell": {"x": cx, "y": cy},
        "color": list(rgb),
        "fragment_position": (
            {"x": source_position[0], "y": source_position[1]}
            if source_position else None),
    }
    return payload


@app.get("/api/drafts/{draft_id}")
async def get_draft(draft_id: int = Path(..., ge=1)):
    row = store().get_draft(draft_id)
    if row is None:
        raise HTTPException(status_code=404, detail={
            "error": "not_found", "message": f"草案 {draft_id} 不存在"})
    return store().row_to_dict(row)


@app.get("/api/drafts/{draft_id}/pattern.png")
async def get_pattern(
    draft_id: int = Path(..., ge=1),
    scale: int = Query(1, ge=1, le=40, description="输出放大倍数"),
):
    row = store().get_draft(draft_id)
    if row is None:
        raise HTTPException(status_code=404, detail={
            "error": "not_found", "message": f"草案 {draft_id} 不存在"})

    if row["status"] != STATUS_OPTIMAL:
        raise HTTPException(status_code=409, detail={
            "error": "not_optimal",
            "message": "草案尚未证明最优，不能领取正式纹版",
            "draft_id": draft_id,
            "status": row["status"],
        })

    grid = DraftStore.decode_grid(row["grid"])
    fixed = DraftStore.decode_points(row["fixed_points"])
    assert grid is not None

    # 正式纹版必须符合当前全部色点
    mismatches = [
        {"cell": {"x": x, "y": y}, "expected": list(color),
         "actual": list(grid[y][x])}
        for (x, y), color in sorted(fixed.items())
        if grid[y][x] != color
    ]
    if mismatches:
        raise HTTPException(status_code=409, detail={
            "error": "point_mismatch",
            "message": "纹版与当前色点不一致，拒绝发放",
            "draft_id": draft_id,
            "mismatches": mismatches,
        })

    png = grid_to_png(grid, scale=scale)
    return Response(
        content=png, media_type="image/png",
        headers={
            "Content-Disposition": (
                f'attachment; filename="pattern_{draft_id}.png"'),
            "X-Draft-Id": str(draft_id),
            "X-Pattern-Score": str(row["score"]),
        },
    )


# --------------------------------------------------------------------------- #
# 裁样单端点
# --------------------------------------------------------------------------- #

@app.post("/api/cut-orders", status_code=201)
async def create_cut_order(body: CutOrderCreate):
    """凭草案编号开裁样单，固定当时可领取的正式纹版。"""
    row = store().get_draft(body.draft_id)
    if row is None:
        raise HTTPException(status_code=404, detail={
            "error": "not_found",
            "message": f"草案 {body.draft_id} 不存在"})

    if row["status"] != STATUS_OPTIMAL:
        raise CutUnavailableError({
            "error": "not_optimal",
            "message": "草案尚未证明最优，不能领取正式纹版开裁样单",
            "draft_id": body.draft_id,
            "status": row["status"],
        })

    grid = DraftStore.decode_grid(row["grid"])
    assert grid is not None
    # 与 pattern.png 相同：正式纹版必须符合当前全部色点
    fixed = DraftStore.decode_points(row["fixed_points"])
    mismatches = [
        {"cell": {"x": x, "y": y}, "expected": list(color),
         "actual": list(grid[y][x])}
        for (x, y), color in sorted(fixed.items())
        if grid[y][x] != color
    ]
    if mismatches:
        raise CutUnavailableError({
            "error": "point_mismatch",
            "message": "纹版与当前色点不一致，不能开裁样单",
            "draft_id": body.draft_id,
            "mismatches": mismatches,
        })

    palette = tuple(tuple(c) for c in json.loads(row["palette"]))  # type: ignore[misc]
    background = _normalize_color(body.background)
    if background not in palette:
        raise CutValidationError(
            f"底色 {list(background)} 不在参考图调色板 "
            f"{[list(c) for c in palette]} 中")

    center_raw = body.center
    if not isinstance(center_raw, dict) or "x" not in center_raw \
            or "y" not in center_raw:
        raise CutValidationError("center 需要 {'x': 整数, 'y': 整数}")
    center = (center_raw["x"], center_raw["y"])
    for value in center:
        if isinstance(value, bool) or not isinstance(value, int):
            raise CutValidationError("花心坐标必须是整数格坐标")

    candidates = validate_candidate_map(
        body.candidates, center, require_all=True)
    cuts: dict[str, int] = {}

    result, trace, withdrawn = _recompute(
        grid=grid, background=background, center=center,
        candidates=candidates, cuts=cuts, previous_plan=None,
    )
    order_id = store().create_cut_order(
        draft_id=body.draft_id,
        unit_w=row["unit_w"], unit_h=row["unit_h"],
        palette=palette, grid=grid, background=background,
        center=center, candidates=candidates, cuts=cuts,
        plan=_stored_plan(result),
        plan_status=(
            PLAN_INFINITE if trace.infinite
            else PLAN_PLANNED if result.ok else PLAN_NO_PLAN),
        plan_reason=result.reason,
        plan_detail=result.detail,
    )
    stored = _load_cut_order(order_id)
    return _cut_order_payload(
        stored, plan_result=result, trace=trace, cuts=cuts,
        withdrawn=withdrawn, candidates=candidates)


@app.get("/api/cut-orders/{order_id}")
async def get_cut_order(order_id: int = Path(..., ge=1)):
    """查看裁样单：使用已落库的方案，重启后也不重算、不改写。"""
    row = _load_cut_order(order_id)
    candidates, cuts, grid, background, center = _stored_cut_state(row)
    result = _plan_result_from_row(row)
    trace = trace_flower(grid, background, center)
    withdrawn = json.loads(row["withdrawn"])
    return _cut_order_payload(
        row, plan_result=result, trace=trace, cuts=cuts,
        withdrawn=withdrawn, candidates=candidates)


@app.post("/api/cut-orders/{order_id}/cuts")
async def register_cut(body: CutRegisterRequest,
                       order_id: int = Path(..., ge=1)):
    """凭单号登记一条已按方案下刀的边；始终沿用已切位置重算。"""
    row = _load_cut_order(order_id)
    candidates, cuts, grid, background, center = _stored_cut_state(row)
    previous_plan = json.loads(row["plan"]) if row["plan"] else None

    edge = body.edge
    if edge in cuts:
        raise ConflictError({
            "error": "edge_already_cut",
            "message": f"边 {edge}（{EDGE_LABELS[edge]}）已登记下刀",
            "order_id": order_id,
            "edge": edge,
            "line": cuts[edge],
        })

    # 当前方案必须可用，且登记的刀线必须与方案一致
    if not previous_plan or not previous_plan.get("available"):
        raise ConflictError({
            "error": "no_plan",
            "message": "当前没有可执行的裁片方案，无法登记下刀",
            "order_id": order_id,
            "plan_reason": row["plan_reason"],
        })
    plan_lines = previous_plan.get("grid_lines") or {}
    expected = plan_lines.get(edge)
    if expected is None:
        raise ConflictError({
            "error": "not_in_plan",
            "message": f"当前方案未包含边 {edge}（{EDGE_LABELS[edge]}）",
            "order_id": order_id,
        })
    line = body.line if body.line is not None else expected
    if line != expected:
        raise ConflictError({
            "error": "line_mismatch",
            "message": "登记的刀线与当前方案不一致",
            "order_id": order_id,
            "edge": edge,
            "expected_line": expected,
            "requested_line": line,
        })

    new_cuts = dict(cuts)
    new_cuts[edge] = line
    result, trace, withdrawn = _recompute(
        grid=grid, background=background, center=center,
        candidates=candidates, cuts=new_cuts, previous_plan=previous_plan,
        previous_withdrawn=(
            json.loads(row["withdrawn"]) if row["withdrawn"] else None),
    )
    store().update_cut_order(
        order_id, candidates=candidates, cuts=new_cuts,
        plan=_stored_plan(result),
        plan_status=(
            PLAN_INFINITE if trace.infinite
            else PLAN_PLANNED if result.ok else PLAN_NO_PLAN),
        plan_reason=result.reason, plan_detail=result.detail,
        withdrawn=withdrawn,
    )
    stored = _load_cut_order(order_id)
    payload = _cut_order_payload(
        stored, plan_result=result, trace=trace, cuts=new_cuts,
        withdrawn=withdrawn, candidates=candidates)
    payload["registered"] = {
        "edge": edge,
        "line": line,
        "cut_count": len(new_cuts),
    }
    return payload


@app.patch("/api/cut-orders/{order_id}/candidates")
async def patch_candidates(body: CandidatesPatchRequest,
                           order_id: int = Path(..., ge=1)):
    """修改一至多条边的候选刀位并重算；已下刀的边仍用已切位置。"""
    row = _load_cut_order(order_id)
    candidates, cuts, grid, background, center = _stored_cut_state(row)
    previous_plan = json.loads(row["plan"]) if row["plan"] else None

    if not body.candidates:
        raise CutValidationError("candidates 至少需要一条边")
    # 已下刀的边不允许改候选刀位（先于数值校验，避免误报方向/距离）
    for edge in body.candidates:
        if edge in cuts:
            raise ConflictError({
                "error": "edge_already_cut",
                "message": (
                    f"边 {edge}（{EDGE_LABELS[edge]}）已下刀，"
                    "候选刀位不能再改"),
                "order_id": order_id,
                "edge": edge,
            })
    updates = validate_candidate_map(
        body.candidates, center, require_all=False)

    merged = {edge: list(candidates.get(edge) or []) for edge in EDGES}
    merged.update(updates)

    result, trace, withdrawn = _recompute(
        grid=grid, background=background, center=center,
        candidates=merged, cuts=cuts, previous_plan=previous_plan,
        previous_withdrawn=(
            json.loads(row["withdrawn"]) if row["withdrawn"] else None),
    )
    store().update_cut_order(
        order_id, candidates=merged, cuts=cuts,
        plan=_stored_plan(result),
        plan_status=(
            PLAN_INFINITE if trace.infinite
            else PLAN_PLANNED if result.ok else PLAN_NO_PLAN),
        plan_reason=result.reason, plan_detail=result.detail,
        withdrawn=withdrawn,
    )
    stored = _load_cut_order(order_id)
    payload = _cut_order_payload(
        stored, plan_result=result, trace=trace, cuts=cuts,
        withdrawn=withdrawn, candidates=merged)
    payload["updated_edges"] = sorted(updates)
    return payload
