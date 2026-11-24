"""裁样（cut-plan）核心算法。

场景
====
制样员要从成品织带上剪下一朵完整的花，同时不能把邻花剪断。
纹版按重复单元 ``W × H`` 无限平铺，因此花朵可能跨单元边界，也可能随平铺
无限延伸（例如整条竖条纹）。

约定
----
* 纹版格 ``(x, y)`` 的颜色为 ``grid[y % H][x % W]``，坐标可以是任意整数
  （允许负数），即无限平铺平面上的坐标。
* ``background``（底色）是参考图调色板中的一种颜色；非底色格视为花纹。
* 花朵 = 平铺平面上与花心格 4 邻接相连的非底色格集合。
* 刀位是**格线坐标**：``x = v`` 这条竖线分隔格 ``v-1`` 与格 ``v``；
  ``y = v`` 这条横线分隔格 ``v-1`` 与格 ``v``。
* 裁片矩形用 ``left / right / top / bottom`` 四条格线表示，包含的格为
  ``left ≤ x < right``、``top ≤ y < bottom``。
* 每条边的刀线只能取自该边的候选刀位；已登记为“已切”的边使用登记时的
  刀线，即使后来候选刀位被改掉也不再变动。
* 矩形必须包住整朵花；并且矩形任一边两侧相邻的两个格不能同时为非底色
  （否则会把一朵花拦腰切断）。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Iterable, Mapping, Optional, Sequence

from .images import RGB

EDGES = ("top", "bottom", "left", "right")
EDGE_LABELS = {
    "top": "上",
    "bottom": "下",
    "left": "左",
    "right": "右",
}

MAX_CANDIDATES_PER_EDGE = 12
MAX_KNIFE_DISTANCE = 32


class CutValidationError(ValueError):
    """裁样参数不符合约定（对应 HTTP 400）。"""


Grid = tuple[tuple[RGB, ...], ...]


# --------------------------------------------------------------------------- #
# 花朵连通块追踪
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class FlowerTrace:
    """花心所在非底色连通块在平铺平面上的追踪结果。"""

    infinite: bool
    center_is_background: bool = False
    cell_count: Optional[int] = None
    x_min: Optional[int] = None
    x_max: Optional[int] = None
    y_min: Optional[int] = None
    y_max: Optional[int] = None
    # 检测到无限延伸时，记录首次撞见的「同一余数、不同平移」证据
    infinite_residue: Optional[tuple[int, int]] = None
    infinite_offsets: Optional[tuple[tuple[int, int], tuple[int, int]]] = None

    @property
    def bounds(self) -> Optional[dict[str, int]]:
        if self.infinite or self.x_min is None:
            return None
        return {
            "x_min": self.x_min,
            "x_max": self.x_max,
            "y_min": self.y_min,
            "y_max": self.y_max,
        }


def trace_flower(grid: Grid, background: RGB, center: tuple[int, int]) -> FlowerTrace:
    """追踪与 ``center`` 4 邻接相连的非底色格（跨单元、允许负坐标）。

    纹版无限平铺且以 ``W × H`` 为周期。把平面格按 ``(x mod W, y mod H)``
    归为余数类，并记录其平移量 ``(x // W, y // H)``。若同一个余数类在
    连通块里以两个不同平移量出现，则连通块必然沿该方向无限重复，直接判定
    为无限延伸；否则连通块最多含 ``W × H`` 个格，搜索必然终止。
    """
    if not grid or not grid[0]:
        raise CutValidationError("纹版网格为空")
    height = len(grid)
    width = len(grid[0])
    cx, cy = center

    def color_at(x: int, y: int) -> RGB:
        return grid[y % height][x % width]

    if color_at(cx, cy) == background:
        return FlowerTrace(infinite=False, center_is_background=True)

    seen: dict[tuple[int, int], tuple[int, int]] = {
        (cx % width, cy % height): (cx // width, cy // height),
    }
    queue: deque[tuple[int, int]] = deque([(cx, cy)])
    count = 0
    x_min = x_max = cx
    y_min = y_max = cy

    while queue:
        x, y = queue.popleft()
        count += 1
        x_min = min(x_min, x)
        x_max = max(x_max, x)
        y_min = min(y_min, y)
        y_max = max(y_max, y)

        for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if color_at(nx, ny) == background:
                continue
            residue = (nx % width, ny % height)
            offset = (nx // width, ny // height)
            previous = seen.get(residue)
            if previous is not None:
                if previous != offset:
                    return FlowerTrace(
                        infinite=True,
                        infinite_residue=residue,
                        infinite_offsets=(previous, offset),
                    )
                # 同一余数且同一平移量就是同一个格，已访问过
                continue
            seen[residue] = offset
            queue.append((nx, ny))

    return FlowerTrace(
        infinite=False,
        cell_count=count,
        x_min=x_min,
        x_max=x_max,
        y_min=y_min,
        y_max=y_max,
    )


# --------------------------------------------------------------------------- #
# 候选刀位校验
# --------------------------------------------------------------------------- #

def _knife_distance(edge: str, line: int, center: tuple[int, int]) -> int:
    """刀线到花心格的“格数”距离；返回负数表示刀线位于花心的错误一侧。

    花心格 ``(cx, cy)`` 占据 ``[cx, cx+1] × [cy, cy+1]``：

    * 上刀线 ``y = cy`` 贴着花心格上沿，距离 0；
    * 下刀线 ``y = cy + 1`` 贴着花心格下沿，距离 0；
    * 左刀线 ``x = cx``、右刀线 ``x = cx + 1`` 同理。
    """
    cx, cy = center
    if edge == "top":
        return cy - line
    if edge == "bottom":
        return line - (cy + 1)
    if edge == "left":
        return cx - line
    if edge == "right":
        return line - (cx + 1)
    raise CutValidationError(f"未知的边：{edge}")


def validate_edge_lines(edge: str, lines: Sequence[int],
                        center: tuple[int, int]) -> list[int]:
    """校验并规范化某一边的候选刀位：去重、排序、数量与距离限制。"""
    if edge not in EDGES:
        raise CutValidationError(
            f"未知的边：{edge}，只支持 {', '.join(EDGES)}")
    if not isinstance(lines, (list, tuple)):
        raise CutValidationError(f"边 {edge} 的候选刀位必须是整数数组")
    normalized: set[int] = set()
    for value in lines:
        if isinstance(value, bool) or not isinstance(value, int):
            raise CutValidationError(
                f"边 {edge} 的候选刀位必须是整数格线坐标，收到 {value!r}")
        distance = _knife_distance(edge, value, center)
        if distance < 0:
            raise CutValidationError(
                f"边 {edge} 的候选刀位 {value} 不在花心对应的一侧")
        if distance > MAX_KNIFE_DISTANCE:
            raise CutValidationError(
                f"边 {edge} 的候选刀位 {value} 距花心 {distance} 格，"
                f"超过上限 {MAX_KNIFE_DISTANCE} 格")
        normalized.add(value)
    if len(normalized) > MAX_CANDIDATES_PER_EDGE:
        raise CutValidationError(
            f"边 {edge} 至多 {MAX_CANDIDATES_PER_EDGE} 个候选刀位，"
            f"收到 {len(normalized)} 个")
    return sorted(normalized)


def validate_candidate_map(raw: Mapping[str, Sequence[int]],
                           center: tuple[int, int],
                           *,
                           require_all: bool) -> dict[str, list[int]]:
    """校验四边候选刀位映射。

    ``require_all=True`` 时四条边必须全部给出（可以为空数组）；
    否则只校验出现的边（用于“改刀位”局部更新）。
    """
    if not isinstance(raw, Mapping):
        raise CutValidationError("candidates 必须是 {边: 刀位数组} 对象")
    unknown = [key for key in raw if key not in EDGES]
    if unknown:
        raise CutValidationError(
            f"未知的边：{', '.join(map(str, unknown))}，"
            f"只支持 {', '.join(EDGES)}")
    if require_all:
        missing = [edge for edge in EDGES if edge not in raw]
        if missing:
            raise CutValidationError(
                f"缺少边的候选刀位：{', '.join(missing)}")
        edges = EDGES
    else:
        edges = tuple(edge for edge in EDGES if edge in raw)
    return {edge: validate_edge_lines(edge, raw[edge], center) for edge in edges}


# --------------------------------------------------------------------------- #
# 最小面积矩形
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Rectangle:
    left: int
    right: int
    top: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    @property
    def area(self) -> int:
        return self.width * self.height

    def as_lines(self) -> dict[str, int]:
        return {
            "left": self.left,
            "right": self.right,
            "top": self.top,
            "bottom": self.bottom,
        }

    def as_cells(self) -> dict[str, int]:
        return {
            "x_min": self.left,
            "x_max": self.right - 1,
            "y_min": self.top,
            "y_max": self.bottom - 1,
        }


@dataclass(frozen=True)
class PlanResult:
    rectangle: Optional[Rectangle]
    reason: Optional[str] = None
    detail: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.rectangle is not None


def _boundary_ok(grid: Grid, background: RGB, rect: Rectangle) -> bool:
    """矩形四边两侧相邻的格不能同时为非底色。"""
    height = len(grid)
    width = len(grid[0])

    def non_background(x: int, y: int) -> bool:
        return grid[y % height][x % width] != background

    for x in range(rect.left, rect.right):
        # 上边：内侧 (x, top)，外侧 (x, top-1)
        if (non_background(x, rect.top)
                and non_background(x, rect.top - 1)):
            return False
        # 下边：内侧 (x, bottom-1)，外侧 (x, bottom)
        if (non_background(x, rect.bottom - 1)
                and non_background(x, rect.bottom)):
            return False
    for y in range(rect.top, rect.bottom):
        # 左边：内侧 (left, y)，外侧 (left-1, y)
        if (non_background(rect.left, y)
                and non_background(rect.left - 1, y)):
            return False
        # 右边：内侧 (right-1, y)，外侧 (right, y)
        if (non_background(rect.right - 1, y)
                and non_background(rect.right, y)):
            return False
    return True


def select_rectangle(
    grid: Grid,
    background: RGB,
    trace: FlowerTrace,
    candidates: Mapping[str, Sequence[int]],
    cut_edges: Mapping[str, int],
) -> PlanResult:
    """在四边候选刀位中挑出包住整朵花的最小面积矩形。

    * 已切边（``cut_edges``）的刀线固定，不受候选刀位变化影响；
    * 未切边只能从候选刀位中选；
    * 矩形必须包含花朵连通块的包围盒；
    * 任一候选组合违反“邻花不剪断”约束即淘汰；
    * 最小面积并列时按 ``(top, left, bottom, right)`` 字典序取最小，保证确定性。
    """
    if trace.center_is_background:
        return PlanResult(None, "center_is_background",
                          "花心格是底色，请改选花纹格")
    if trace.infinite:
        return PlanResult(None, "flower_infinite",
                          "花纹随平铺无限延伸，无法裁出一朵完整的花")
    assert trace.x_min is not None
    assert trace.x_max is not None
    assert trace.y_min is not None
    assert trace.y_max is not None

    options: dict[str, list[int]] = {}
    for edge in EDGES:
        if edge in cut_edges:
            options[edge] = [cut_edges[edge]]
            continue
        values = list(candidates.get(edge) or [])
        if not values:
            return PlanResult(
                None, f"no_candidates:{edge}",
                f"边 {edge}（{EDGE_LABELS[edge]}）没有可用的候选刀位")
        options[edge] = values

    # 先用花朵包围盒过滤：任何一边都没法包住花朵时直接判定无解
    bound_filters = {
        "top": lambda v: v <= trace.y_min,
        "bottom": lambda v: v >= trace.y_max + 1,
        "left": lambda v: v <= trace.x_min,
        "right": lambda v: v >= trace.x_max + 1,
    }
    for edge in EDGES:
        kept = [v for v in options[edge] if bound_filters[edge](v)]
        if not kept:
            if edge in cut_edges:
                return PlanResult(
                    None, f"cut_line_excludes_flower:{edge}",
                    f"已切刀线（边 {edge}）包不住整朵花，无法得到完整裁片")
            return PlanResult(
                None, f"no_line_contains_flower:{edge}",
                f"边 {edge}（{EDGE_LABELS[edge]}）的候选刀位都包不住整朵花")
        options[edge] = kept

    best: Optional[Rectangle] = None
    best_key: Optional[tuple[int, int, int, int, int]] = None
    for top in options["top"]:
        for left in options["left"]:
            for bottom in options["bottom"]:
                for right in options["right"]:
                    if right <= left or bottom <= top:
                        continue
                    rect = Rectangle(left=left, right=right,
                                     top=top, bottom=bottom)
                    key = (rect.area, top, left, bottom, right)
                    if best_key is not None and key >= best_key:
                        continue
                    if not _boundary_ok(grid, background, rect):
                        continue
                    best = rect
                    best_key = key

    if best is None:
        return PlanResult(
            None, "boundary_conflict",
            "找不到既包住整朵花、又不会剪断邻花的刀位组合")
    return PlanResult(best)


def rectangle_payload(rect: Rectangle) -> dict:
    return {
        "grid_lines": rect.as_lines(),
        "cell_bounds": rect.as_cells(),
        "width": rect.width,
        "height": rect.height,
        "area": rect.area,
    }


def edge_order(edges: Iterable[str]) -> list[str]:
    present = set(edges)
    return [edge for edge in EDGES if edge in present]
