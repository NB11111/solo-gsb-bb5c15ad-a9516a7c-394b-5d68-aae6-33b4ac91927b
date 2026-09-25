"""SRT 版本差异审校测试：DP 单元性质 + /api/diff、/api/diff.csv 接口。

运行：python -m pytest tests/test_diff.py -q
"""
from fastapi.testclient import TestClient

from app.diffalign import GAP_COST, SPLIT_COST, diff_align
from app.main import app
from app.parsers import parse_srt

client = TestClient(app)


def block(i, start, end, text):
    return f"{i}\n{start} --> {end}\n{text}\n"


def make_srt(blocks):
    """blocks: [(start_tc, end_tc, text), ...]，序号从 1 自动生成。"""
    return "\n".join(
        block(i, s, e, t)
        for i, (s, e, t) in enumerate(blocks, 1)
    )


OLD = make_srt([
    ("00:00:01,000", "00:00:03,000", "你好，世界！"),
    ("00:00:04,000", "00:00:07,000", "这一句在新版被拆成两块。"),
    ("00:00:08,000", "00:00:10,000", "完全没有变化的一行。"),
    ("00:00:11,000", "00:00:13,000", "这一行只调整了时间码。"),
    ("00:00:14,000", "00:00:16,000", "新版里这一句被删除了。"),
    ("00:00:17,000", "00:00:19,000", "新版相邻两块"),
    ("00:00:19,500", "00:00:21,000", "在旧版合并成一块。"),
])

NEW = make_srt([
    ("00:00:01,000", "00:00:03,000", "你好，世界！正文略有修改。"),
    ("00:00:04,000", "00:00:05,500", "这一句在新版"),
    ("00:00:05,800", "00:00:07,000", "被拆成两块。"),
    ("00:00:08,000", "00:00:10,000", "完全没有变化的一行。"),
    ("00:00:11,500", "00:00:13,500", "这一行只调整了时间码。"),
    ("00:00:17,000", "00:00:21,000", "新版相邻两块在旧版合并成一块。"),
    ("00:00:22,000", "00:00:24,000", "这是旧版没有的全新字幕。"),
])


# ---------- DP 单元性质 ----------

def _ops(old_text, new_text):
    return diff_align(parse_srt(old_text), parse_srt(new_text))


def test_identical_versions_all_unchanged():
    ops = _ops(OLD, OLD)
    assert [o[0] for o in ops] == ["match11"] * 7
    assert all(o[1] == 0.0 and o[2] == 0.0 and o[3] == 0 for o in ops)
    # 两侧索引按序一一对应
    assert [(o[4], o[5]) for o in ops] == [(k, k) for k in range(7)]


def test_full_status_set_via_api():
    resp = client.post("/api/diff", json={"old_srt": OLD, "new_srt": NEW})
    assert resp.status_code == 200
    data = resp.json()
    statuses = [r["status"] for r in data["rows"]]
    assert statuses == [
        "text_changed", "split", "unchanged", "time_only",
        "deleted", "merge", "added",
    ]

    s = data["summary"]
    assert s["unchanged"] == 1
    assert s["time_only"] == 1
    assert s["text_changed"] == 1
    assert s["split"] == 1
    assert s["merge"] == 1
    assert s["added"] == 1
    assert s["deleted"] == 1
    assert s["total_rows"] == 7
    assert s["old_total"] == 7 and s["new_total"] == 7
    # 拆分 0.1 + 合并 0.1 + 新增/删除各 0.45
    assert s["total_cost"] == round(2 * SPLIT_COST + 2 * GAP_COST
                                    + data["rows"][0]["cost"], 6)
    # 总代价与逐行代价之和（显示口径）一致
    assert s["total_cost"] == round(sum(r["cost"] for r in data["rows"]), 6)


def test_time_only_row_time_delta():
    row = [r for r in client.post(
        "/api/diff", json={"old_srt": OLD, "new_srt": NEW}
    ).json()["rows"] if r["status"] == "time_only"][0]
    assert row["text_cost"] == 0.0
    assert row["time_delta_ms"] == 1000  # 入点 +500、出点 +500


def test_split_row_carries_two_new_blocks_in_order():
    row = [r for r in client.post(
        "/api/diff", json={"old_srt": OLD, "new_srt": NEW}
    ).json()["rows"] if r["status"] == "split"][0]
    assert [b["index"] for b in row["old"]] == [2]
    assert [b["index"] for b in row["new"]] == [2, 3]
    assert row["new"][0]["start_tc"] == "00:00:04,000"
    assert row["new"][1]["end_tc"] == "00:00:07,000"
    assert row["cost"] == SPLIT_COST  # 合并正文后归一化完全相同


def test_merge_row_carries_two_old_blocks_in_order():
    row = [r for r in client.post(
        "/api/diff", json={"old_srt": OLD, "new_srt": NEW}
    ).json()["rows"] if r["status"] == "merge"][0]
    assert [b["index"] for b in row["old"]] == [6, 7]
    assert [b["index"] for b in row["new"]] == [6]
    assert row["old"][0]["start_tc"] == "00:00:17,000"
    assert row["old"][1]["end_tc"] == "00:00:21,000"
    assert row["cost"] == SPLIT_COST


def test_split_merge_text_is_concatenated_before_normalize():
    # 旧一块「今天 天气」与新两块「今天」「天气」：拆行不影响归一化拼接
    old = make_srt([("00:00:01,000", "00:00:03,000", "今天天气真好")])
    new = make_srt([
        ("00:00:01,000", "00:00:02,000", "今天，天气"),
        ("00:00:02,000", "00:00:03,000", "真好！"),
    ])
    rows = client.post(
        "/api/diff", json={"old_srt": old, "new_srt": new}
    ).json()["rows"]
    assert [r["status"] for r in rows] == ["split"]
    assert rows[0]["text_cost"] == 0.0
    assert rows[0]["cost"] == SPLIT_COST


def test_one_side_empty_is_all_gaps():
    one = make_srt([
        ("00:00:01,000", "00:00:02,000", "甲"),
        ("00:00:03,000", "00:00:04,000", "乙"),
    ])
    r1 = client.post("/api/diff", json={"old_srt": one, "new_srt": ""})
    assert [r["status"] for r in r1.json()["rows"]] == ["deleted", "deleted"]
    assert r1.json()["summary"]["total_cost"] == 2 * GAP_COST
    r2 = client.post("/api/diff", json={"old_srt": "", "new_srt": one})
    assert [r["status"] for r in r2.json()["rows"]] == ["added", "added"]


def test_both_empty_returns_400():
    resp = client.post("/api/diff", json={"old_srt": " ", "new_srt": ""})
    assert resp.status_code == 400


def test_bad_srt_returns_400():
    good = make_srt([("00:00:01,000", "00:00:02,000", "甲")])
    resp = client.post(
        "/api/diff", json={"old_srt": "没有时间码", "new_srt": good}
    )
    assert resp.status_code == 400
    assert "旧版 SRT" in resp.json()["detail"]
    resp = client.post(
        "/api/diff", json={"old_srt": good, "new_srt": "没有时间码"}
    )
    assert resp.status_code == 400
    assert "新版 SRT" in resp.json()["detail"]


# ---------- 平局破平 ----------

def test_tie_prefers_smaller_time_delta():
    # 精确同分：dist(a,"aba"+"ab")+0.1 == dist(a,"aba")+0.45 == 0.7
    # （a="aaba"：拆分文字代价 0.6，1:1 文字代价 0.25）
    a, b1, b2 = "aaba", "aba", "ab"
    old = make_srt([("00:00:01,000", "00:00:04,000", a)])
    # 拆分路径 dt=5000（b2 出点 9s），1:1+新增路径 dt=2000 → 选 1:1
    new_far = make_srt([
        ("00:00:01,000", "00:00:02,000", b1),
        ("00:00:03,000", "00:00:09,000", b2),
    ])
    ops_far = _ops(old, new_far)
    assert [o[0] for o in ops_far] == ["match11", "gap_new"]
    assert round(sum(o[1] for o in ops_far), 9) == 0.7
    # 拆分路径 dt=0 时优先拆分（总分同为 0.7）
    new_near = make_srt([
        ("00:00:01,000", "00:00:02,000", b1),
        ("00:00:03,000", "00:00:04,000", b2),
    ])
    ops_near = _ops(old, new_near)
    assert [o[0] for o in ops_near] == ["split"]
    assert round(ops_near[0][1], 9) == 0.7


def test_tie_prefers_one_to_one_shape():
    # 旧两块（11a / 11b），新一块（9a），时间完全对齐：
    # 1:1+删除 与 合并+删除 总分相同（文字 2/11≈0.1818 + 0.45），
    # 入出点偏差同为 0；第三级偏好 1:1
    old = make_srt([
        ("00:00:01,000", "00:00:02,000", "a" * 11),
        ("00:00:03,000", "00:00:04,000", "b" * 11),
    ])
    new = make_srt([("00:00:01,000", "00:00:02,000", "a" * 9)])
    ops = _ops(old, new)
    assert [o[0] for o in ops] == ["match11", "gap_old"]


# ---------- 全局性质 ----------

def test_monotone_and_each_block_used_once():
    ops = _ops(OLD, NEW)
    old_used, new_used = [], []
    for op, _cost, _tc, _dt, i, j in ops:
        if i is not None:
            idxs = [i, i + 1] if op == "merge" else [i]
            old_used.extend(idxs)
        if j is not None:
            idxs = [j, j + 1] if op == "split" else [j]
            new_used.extend(idxs)
    # 每块恰好出现一次
    assert sorted(old_used) == list(range(7))
    assert sorted(new_used) == list(range(7))
    # 顺序单调
    assert old_used == sorted(old_used)
    assert new_used == sorted(new_used)


# ---------- CSV ----------

def test_csv_export():
    resp = client.post("/api/diff.csv", json={"old_srt": OLD, "new_srt": NEW})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "srt-diff.csv" in resp.headers["content-disposition"]
    assert resp.content[:3] == b"\xef\xbb\xbf"  # UTF-8 BOM
    text = resp.content.decode("utf-8-sig")
    header = text.splitlines()[0]
    assert header.startswith("状态,代价,文字代价,入出点偏差(ms)")
    for label in ("未变", "仅改时", "文字改动", "拆分", "合并", "新增", "删除"):
        assert label in text
    # 拆分块新版序号与两段时间码都在同一行
    split_line = next(ln for ln in text.splitlines()[1:] if ln.startswith("拆分"))
    assert "2；3" in split_line
    assert "00:00:04,000；00:00:05,800" in split_line
