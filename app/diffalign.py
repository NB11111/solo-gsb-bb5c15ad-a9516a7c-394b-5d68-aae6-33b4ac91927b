"""SRT 版本差异对齐：按块序做全局最小代价对齐。

允许的对位关系（两侧各块至多参与一次，整体顺序单调）：

- 1:1：旧版一块对新版一块；
- 1:2（拆分）：旧版一块对新版连续两块；
- 2:1（合并）：旧版连续两块对新版一块；
- 单侧缺项：旧有新无（删除）或新有旧无（新增）。

计分：候选先把参与块的正文合并，再复用 textnorm 的归一化与归一化
字符编辑距离得到文字代价；拆分/合并在文字代价之外另加 SPLIT_COST
（0.1）；单侧缺项（增/删）各计 GAP_COST（0.45）。

总分相同时按以下顺序逐级破平：

1. 入出点偏差之和（毫秒）更小：取配对两侧首块入点差绝对值与
   末块出点差绝对值之和（1:1 即 |入-入|+|出-出|；缺项为 0）；
2. 再相同则优先 1:1：非 1:1 操作（拆分/合并/缺项）每发生一次计 1。

优先使用 numpy + rapidfuzz 批量计算距离矩阵；缺少 numpy 时自动回退
到纯 Python 实现，结果完全一致，仅速度较慢。
"""
from __future__ import annotations

from .parsers import SrtItem
from .textnorm import normalize_text, normalized_distance

GAP_COST = 0.45
SPLIT_COST = 0.1

# (操作, 总代价, 文字代价, 时间偏差ms, 旧侧索引, 新侧索引)
# match11：i、j 各一块；split：旧 i 一块对新 j、j+1 两块；
# merge：旧 i、i+1 两块对新 j 一块；
# gap_old（删除）：j 为 None；gap_new（新增）：i 为 None。
DiffOp = tuple[str, float, float, int, int | None, int | None]

# 回溯方向：1=1:1 2=删除(上) 3=新增(左) 4=拆分 5=合并
_DIAG, _UP, _LEFT, _SPLIT, _MERGE = 1, 2, 3, 4, 5

# 元组即 (总分, 时间偏差之和, 非1:1计数)，逐级字典序比较
_Cell = tuple[float, int, int]


def _time_delta(a: SrtItem, b: SrtItem, a_end: SrtItem, b_end: SrtItem) -> int:
    """首块入点差绝对值 + 末块出点差绝对值（毫秒）。"""
    return abs(a.start_ms - b.start_ms) + abs(a_end.end_ms - b_end.end_ms)


def _all_gap_old(n: int) -> list[DiffOp]:
    return [
        ("gap_old", GAP_COST, 0.0, 0, i, None) for i in range(n)
    ]


def _all_gap_new(m: int) -> list[DiffOp]:
    return [
        ("gap_new", GAP_COST, 0.0, 0, None, j) for j in range(m)
    ]


try:
    import numpy as np

    # 复用 alignment.py 中 rapidfuzz 批量距离矩阵 + 本项目归一化口径
    from .alignment import _distance_matrix

    def _align_numpy(old: list[SrtItem], new: list[SrtItem]) -> list[DiffOp]:
        n, m = len(old), len(new)
        norms_a = [normalize_text(it.raw_text) for it in old]
        norms_b = [normalize_text(it.raw_text) for it in new]

        # 1:1 / 1:2 / 2:1 三类候选的文字代价矩阵
        d11 = _distance_matrix(norms_a, norms_b)
        d12 = (
            _distance_matrix(norms_a, [norms_b[j] + norms_b[j + 1] for j in range(m - 1)])
            if m >= 2 else np.zeros((n, 0))
        )
        d21 = (
            _distance_matrix([norms_a[i] + norms_a[i + 1] for i in range(n - 1)], norms_b)
            if n >= 2 else np.zeros((0, m))
        )

        sa = np.array([it.start_ms for it in old], dtype=np.int64)
        ea = np.array([it.end_ms for it in old], dtype=np.int64)
        sb = np.array([it.start_ms for it in new], dtype=np.int64)
        eb = np.array([it.end_ms for it in new], dtype=np.int64)
        # 1:1：|入a-入b| + |出a-出b|
        t11 = (np.abs(sa[:, None] - sb[None, :])
               + np.abs(ea[:, None] - eb[None, :]))
        # 1:2（拆分）：旧 i 对新 j、j+1 —— 入点比首块，出点比第二块
        t12 = (
            np.abs(sa[:, None] - sb[:-1][None, :])
            + np.abs(ea[:, None] - eb[1:][None, :])
            if m >= 2 else np.zeros((n, 0), dtype=np.int64)
        )
        # 2:1（合并）：旧 i、i+1 对新 j
        t21 = (
            np.abs(sa[:-1][:, None] - sb[None, :])
            + np.abs(ea[1:][:, None] - eb[None, :])
            if n >= 2 else np.zeros((0, m), dtype=np.int64)
        )

        back = [bytearray(m + 1) for _ in range(n + 1)]

        # 边界行/列：只能由连续缺项到达
        prev: list[_Cell] = [(j * GAP_COST, 0, j) for j in range(m + 1)]
        back[0][1:] = bytes([_LEFT]) * m
        prev2: list[_Cell] | None = None  # i-2 行，i=1 时不存在

        for i in range(1, n + 1):
            cur: list[_Cell] = [(0, 0, 0)] * (m + 1)
            cur[0] = (i * GAP_COST, 0, i)
            back[i][0] = _UP
            row = back[i]

            d11r = d11[i - 1].tolist()
            t11r = t11[i - 1].tolist()
            d12r = d12[i - 1].tolist() if m >= 2 else []
            t12r = t12[i - 1].tolist() if m >= 2 else []
            can_merge = i >= 2
            if can_merge:
                d21r = d21[i - 2].tolist()
                t21r = t21[i - 2].tolist()

            for j in range(1, m + 1):
                # 候选项顺序仅在三级破平后仍完全平局时生效（确定性输出）
                diag = (
                    prev[j - 1][0] + d11r[j - 1],
                    prev[j - 1][1] + t11r[j - 1],
                    prev[j - 1][2],
                )
                best = diag
                direction = _DIAG

                up = (prev[j][0] + GAP_COST, prev[j][1], prev[j][2] + 1)
                if up < best:
                    best, direction = up, _UP

                left = (cur[j - 1][0] + GAP_COST, cur[j - 1][1], cur[j - 1][2] + 1)
                if left < best:
                    best, direction = left, _LEFT

                if j >= 2:
                    split = (
                        prev[j - 2][0] + d12r[j - 2] + SPLIT_COST,
                        prev[j - 2][1] + t12r[j - 2],
                        prev[j - 2][2] + 1,
                    )
                    if split < best:
                        best, direction = split, _SPLIT

                if can_merge:
                    merge = (
                        prev2[j - 1][0] + d21r[j - 1] + SPLIT_COST,
                        prev2[j - 1][1] + t21r[j - 1],
                        prev2[j - 1][2] + 1,
                    )
                    if merge < best:
                        best, direction = merge, _MERGE

                cur[j] = best
                row[j] = direction

            prev2, prev = prev, cur

        ops: list[DiffOp] = []
        i, j = n, m
        d11l, t11l = d11.tolist(), t11.tolist()
        d12l, t12l = d12.tolist(), t12.tolist()
        d21l, t21l = d21.tolist(), t21.tolist()
        while i > 0 or j > 0:
            direction = back[i][j]
            if direction == _DIAG:
                text_cost = float(d11l[i - 1][j - 1])
                ops.append((
                    "match11", text_cost, text_cost,
                    int(t11l[i - 1][j - 1]), i - 1, j - 1,
                ))
                i -= 1
                j -= 1
            elif direction == _UP:
                ops.append(("gap_old", GAP_COST, 0.0, 0, i - 1, None))
                i -= 1
            elif direction == _LEFT:
                ops.append(("gap_new", GAP_COST, 0.0, 0, None, j - 1))
                j -= 1
            elif direction == _SPLIT:
                text_cost = float(d12l[i - 1][j - 2])
                ops.append((
                    "split", text_cost + SPLIT_COST, text_cost,
                    int(t12l[i - 1][j - 2]), i - 1, j - 2,
                ))
                i -= 1
                j -= 2
            else:  # _MERGE
                text_cost = float(d21l[i - 2][j - 1])
                ops.append((
                    "merge", text_cost + SPLIT_COST, text_cost,
                    int(t21l[i - 2][j - 1]), i - 2, j - 1,
                ))
                i -= 2
                j -= 1
        ops.reverse()
        return ops

    _HAVE_NUMPY = True
except ImportError:  # pragma: no cover - 取决于运行环境
    _HAVE_NUMPY = False


def _align_python(old: list[SrtItem], new: list[SrtItem]) -> list[DiffOp]:
    n, m = len(old), len(new)
    norms_a = [normalize_text(it.raw_text) for it in old]
    norms_b = [normalize_text(it.raw_text) for it in new]

    def text_cost(kind: str, i: int, j: int) -> float:
        if kind == "match11":
            return normalized_distance(norms_a[i], norms_b[j])
        if kind == "split":
            return normalized_distance(norms_a[i], norms_b[j] + norms_b[j + 1])
        return normalized_distance(norms_a[i] + norms_a[i + 1], norms_b[j])

    back: list[bytearray] = [bytearray(m + 1) for _ in range(n + 1)]
    prev: list[_Cell] = [(j * GAP_COST, 0, j) for j in range(m + 1)]
    back[0][1:] = bytes([_LEFT]) * m
    prev2: list[_Cell] | None = None

    for i in range(1, n + 1):
        cur: list[_Cell] = [(0, 0, 0)] * (m + 1)
        cur[0] = (i * GAP_COST, 0, i)
        back[i][0] = _UP
        row = back[i]
        for j in range(1, m + 1):
            c = text_cost("match11", i - 1, j - 1)
            t = _time_delta(old[i - 1], new[j - 1], old[i - 1], new[j - 1])
            best: _Cell = (prev[j - 1][0] + c, prev[j - 1][1] + t, prev[j - 1][2])
            direction = _DIAG

            up = (prev[j][0] + GAP_COST, prev[j][1], prev[j][2] + 1)
            if up < best:
                best, direction = up, _UP

            left = (cur[j - 1][0] + GAP_COST, cur[j - 1][1], cur[j - 1][2] + 1)
            if left < best:
                best, direction = left, _LEFT

            if j >= 2:
                c = text_cost("split", i - 1, j - 2)
                t = _time_delta(old[i - 1], new[j - 2], old[i - 1], new[j - 1])
                split = (
                    prev[j - 2][0] + c + SPLIT_COST,
                    prev[j - 2][1] + t,
                    prev[j - 2][2] + 1,
                )
                if split < best:
                    best, direction = split, _SPLIT

            if i >= 2:
                c = text_cost("merge", i - 2, j - 1)
                t = _time_delta(old[i - 2], new[j - 1], old[i - 1], new[j - 1])
                merge = (
                    prev2[j - 1][0] + c + SPLIT_COST,
                    prev2[j - 1][1] + t,
                    prev2[j - 1][2] + 1,
                )
                if merge < best:
                    best, direction = merge, _MERGE

            cur[j] = best
            row[j] = direction
        prev2, prev = prev, cur

    ops: list[DiffOp] = []
    i, j = n, m
    while i > 0 or j > 0:
        direction = back[i][j]
        if direction == _DIAG:
            c = text_cost("match11", i - 1, j - 1)
            t = _time_delta(old[i - 1], new[j - 1], old[i - 1], new[j - 1])
            ops.append(("match11", c, c, t, i - 1, j - 1))
            i -= 1
            j -= 1
        elif direction == _UP:
            ops.append(("gap_old", GAP_COST, 0.0, 0, i - 1, None))
            i -= 1
        elif direction == _LEFT:
            ops.append(("gap_new", GAP_COST, 0.0, 0, None, j - 1))
            j -= 1
        elif direction == _SPLIT:
            c = text_cost("split", i - 1, j - 2)
            t = _time_delta(old[i - 1], new[j - 2], old[i - 1], new[j - 1])
            ops.append(("split", c + SPLIT_COST, c, t, i - 1, j - 2))
            i -= 1
            j -= 2
        else:
            c = text_cost("merge", i - 2, j - 1)
            t = _time_delta(old[i - 2], new[j - 1], old[i - 1], new[j - 1])
            ops.append(("merge", c + SPLIT_COST, c, t, i - 2, j - 1))
            i -= 2
            j -= 1
    ops.reverse()
    return ops


def diff_align(old: list[SrtItem], new: list[SrtItem]) -> list[DiffOp]:
    """对两版 SRT 做全局最小代价差异对齐。

    返回按两侧块序展开的 DiffOp 列表（见模块开头的类型说明）。
    """
    if not old:
        return _all_gap_new(len(new))
    if not new:
        return _all_gap_old(len(old))
    if _HAVE_NUMPY:
        return _align_numpy(old, new)
    return _align_python(old, new)
