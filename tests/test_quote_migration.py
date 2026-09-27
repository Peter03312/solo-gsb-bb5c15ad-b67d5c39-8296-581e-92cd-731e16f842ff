"""快照字幕引用迁移测试。

覆盖：
- 按源快照冻结生效文本提取引用：单字幕内、跨相邻字幕、终点落在下一
  字幕 0 偏移（不含）等边界；快照后的新更正 / 新定稿不影响提取；
- 目标快照连续字幕文本中的查找：命中可跨字幕；缺口阻断拼接，跨缺口
  不命中；空字幕不占码点；
- 恰好一处命中 found（返回目标起止序号与码点偏移）、零次 missing、
  多次 ambiguous（含重叠命中，返回 match_count 且不任选位置）；
- 序号越界 / 落在缺口 / 偏移越界 / 空引用 -> 422；源或目标快照不存在
  -> 404；跨频道 -> 409；请求体校验（start_seq>end_seq、同字幕偏移
  倒置、负数等）-> 422；
- Unicode 码点偏移：非 BMP 字符按单码点计，组合字符不做归一化；
- 只读：快照与当前定稿 / 更正均不被修改，重复调用结果一致。
"""


def submit(client, channel, seq, revision, text="t"):
    resp = client.post(
        f"/channels/{channel}/fragments",
        json={"seq": seq, "revision": revision, "text": text},
    )
    assert resp.status_code == 200
    return resp.json()


def decide_gap(client, channel, start_seq, end_seq):
    resp = client.post(
        f"/channels/{channel}/gaps",
        json={"start_seq": start_seq, "end_seq": end_seq},
    )
    assert resp.status_code == 200
    return resp.json()


def correct(client, channel, seq, expected_version, text, reason="r"):
    resp = client.post(
        f"/channels/{channel}/finalized/{seq}/corrections",
        json={
            "expected_version": expected_version,
            "text": text,
            "reason": reason,
        },
    )
    assert resp.status_code == 200
    return resp.json()


def make_snapshot(client, channel, key):
    resp = client.post(
        f"/channels/{channel}/snapshots", json={"idempotency_key": key}
    )
    assert resp.status_code == 200
    return resp.json()


def migrate(client, source_id, target_id, start_seq, start_offset, end_seq, end_offset):
    return client.post(
        f"/snapshots/{source_id}/quotes/migrate",
        json={
            "target_snapshot_id": target_id,
            "start_seq": start_seq,
            "start_offset": start_offset,
            "end_seq": end_seq,
            "end_offset": end_offset,
        },
    )


# ---------- 基本迁移语义 ----------


def test_migrate_quote_found_within_single_subtitle(client):
    submit(client, "c1", 1, 1, "hello world")
    snap_a = make_snapshot(client, "c1", "a")
    submit(client, "c1", 2, 1, "second")
    snap_b = make_snapshot(client, "c1", "b")

    resp = migrate(client, snap_a["snapshot_id"], snap_b["snapshot_id"], 1, 6, 1, 11)
    assert resp.status_code == 200
    body = resp.json()
    assert body["source_snapshot_id"] == snap_a["snapshot_id"]
    assert body["target_snapshot_id"] == snap_b["snapshot_id"]
    assert body["channel"] == "c1"
    assert body["quote"] == "world"
    assert body["status"] == "found"
    assert body["match_count"] == 1
    assert body["match"] == {
        "start_seq": 1,
        "start_offset": 6,
        "end_seq": 1,
        "end_offset": 11,
    }


def test_migrate_quote_spans_subtitles_on_both_sides(client):
    submit(client, "c1", 1, 1, "alpha")
    submit(client, "c1", 2, 1, "beta")
    snap_a = make_snapshot(client, "c1", "a")

    # 快照 A 之后：seq1 被更正（原位置不再命中），新增字幕 3-4
    correct(client, "c1", 1, 0, "gamma")
    submit(client, "c1", 3, 1, "xxl")
    submit(client, "c1", 4, 1, "phabeyy")
    snap_b = make_snapshot(client, "c1", "b")

    # 源引用跨 seq1/seq2 拼接处："lpha" + "be" = "lphabe"
    resp = migrate(client, snap_a["snapshot_id"], snap_b["snapshot_id"], 1, 1, 2, 2)
    body = resp.json()
    assert body["quote"] == "lphabe"
    assert body["status"] == "found"
    assert body["match_count"] == 1
    # 目标中唯一命中横跨 seq3/seq4 拼接处（"xxl" + "phabeyy"）
    assert body["match"] == {
        "start_seq": 3,
        "start_offset": 2,
        "end_seq": 4,
        "end_offset": 5,
    }


def test_migrate_quote_onto_same_snapshot(client):
    submit(client, "c1", 1, 1, "unique")
    submit(client, "c1", 2, 1, "text")
    snap = make_snapshot(client, "c1", "a")
    sid = snap["snapshot_id"]

    body = migrate(client, sid, sid, 1, 0, 1, 6).json()
    assert body["status"] == "found"
    assert body["match"] == {
        "start_seq": 1,
        "start_offset": 0,
        "end_seq": 1,
        "end_offset": 6,
    }


def test_migrate_quote_end_offset_zero_on_next_subtitle(client):
    submit(client, "c1", 1, 1, "abc")
    submit(client, "c1", 2, 1, "de")
    snap_a = make_snapshot(client, "c1", "a")
    snap_b = make_snapshot(client, "c1", "b")

    # 终点落在 seq2 的 0 偏移（不含）：引用恰好是 seq1 全文
    body = migrate(client, snap_a["snapshot_id"], snap_b["snapshot_id"], 1, 0, 2, 0).json()
    assert body["quote"] == "abc"
    assert body["status"] == "found"
    # 命中终点归于包含末码点的字幕，而不是下一字幕的 0 偏移
    assert body["match"] == {
        "start_seq": 1,
        "start_offset": 0,
        "end_seq": 1,
        "end_offset": 3,
    }


def test_migrate_quote_hit_ends_exactly_at_subtitle_end(client):
    submit(client, "c1", 1, 1, "xy")
    snap_a = make_snapshot(client, "c1", "a")
    correct(client, "c1", 1, 0, "zz")
    submit(client, "c1", 2, 1, "ab")
    submit(client, "c1", 3, 1, "xy")
    submit(client, "c1", 4, 1, "cd")
    snap_b = make_snapshot(client, "c1", "b")

    # 目标中唯一命中为 seq3 全文，终点恰好落在字幕末尾
    body = migrate(client, snap_a["snapshot_id"], snap_b["snapshot_id"], 1, 0, 1, 2).json()
    assert body["status"] == "found"
    assert body["match"] == {
        "start_seq": 3,
        "start_offset": 0,
        "end_seq": 3,
        "end_offset": 2,
    }


def test_migrate_quote_with_empty_subtitles(client):
    submit(client, "c1", 1, 1, "")
    submit(client, "c1", 2, 1, "ab")
    submit(client, "c1", 3, 1, "")
    submit(client, "c1", 4, 1, "cd")
    snap_a = make_snapshot(client, "c1", "a")
    snap_b = make_snapshot(client, "c1", "b")

    # 空字幕不占码点：跨空字幕的引用照常提取与命中
    body = migrate(client, snap_a["snapshot_id"], snap_b["snapshot_id"], 2, 0, 4, 2).json()
    assert body["quote"] == "abcd"
    assert body["status"] == "found"
    assert body["match"] == {
        "start_seq": 2,
        "start_offset": 0,
        "end_seq": 4,
        "end_offset": 2,
    }


# ---------- missing / ambiguous ----------


def test_migrate_quote_missing(client):
    submit(client, "c1", 1, 1, "abcdef")
    snap_a = make_snapshot(client, "c1", "a")
    correct(client, "c1", 1, 0, "xyz")  # 目标快照中原文已被更正掉
    snap_b = make_snapshot(client, "c1", "b")

    body = migrate(client, snap_a["snapshot_id"], snap_b["snapshot_id"], 1, 0, 1, 3).json()
    assert body["quote"] == "abc"
    assert body["status"] == "missing"
    assert body["match_count"] == 0
    assert body["match"] is None


def test_migrate_quote_empty_target_snapshot(client):
    empty = make_snapshot(client, "c2", "e")
    submit(client, "c2", 1, 1, "abc")
    snap = make_snapshot(client, "c2", "f")

    body = migrate(client, snap["snapshot_id"], empty["snapshot_id"], 1, 0, 1, 3).json()
    assert body["quote"] == "abc"
    assert body["status"] == "missing"
    assert body["match_count"] == 0
    assert body["match"] is None


def test_migrate_quote_ambiguous_counts_overlapping_hits(client):
    submit(client, "c1", 1, 1, "aa")
    snap_a = make_snapshot(client, "c1", "a")
    submit(client, "c1", 2, 1, "aaaa")
    submit(client, "c1", 3, 1, "bb")
    snap_b = make_snapshot(client, "c1", "b")

    body = migrate(client, snap_a["snapshot_id"], snap_b["snapshot_id"], 1, 0, 1, 2).json()
    assert body["quote"] == "aa"
    # 目标拼接 "aa" + "aaaa" + "bb" = "aaaaaabb"："aa" 重叠命中 5 处
    assert body["status"] == "ambiguous"
    assert body["match_count"] == 5
    # 不任选任何一个位置
    assert body["match"] is None


def test_migrate_quote_ambiguous_across_segments(client):
    submit(client, "c1", 1, 1, "abab")
    snap_a = make_snapshot(client, "c1", "a")
    submit(client, "c1", 2, 1, "ab")
    snap_b = make_snapshot(client, "c1", "b")

    # "ab" 在目标拼接 "ababab" 中命中 0、2、4 三位
    body = migrate(client, snap_a["snapshot_id"], snap_b["snapshot_id"], 1, 0, 1, 2).json()
    assert body["status"] == "ambiguous"
    assert body["match_count"] == 3
    assert body["match"] is None


# ---------- 缺口语义 ----------


def test_migrate_quote_gap_blocks_target_matching(client):
    submit(client, "c1", 1, 1, "ab")
    submit(client, "c1", 3, 1, "cd")
    decide_gap(client, "c1", 2, 2)
    submit(client, "c1", 4, 1, "abcd")
    snap_a = make_snapshot(client, "c1", "a")

    # 目标快照：seq4 被更正，"abcd" 只能跨缺口拼出 -> 不命中
    correct(client, "c1", 4, 0, "wxyz")
    snap_b = make_snapshot(client, "c1", "b")

    body = migrate(client, snap_a["snapshot_id"], snap_b["snapshot_id"], 4, 0, 4, 4).json()
    assert body["quote"] == "abcd"
    assert body["status"] == "missing"
    assert body["match_count"] == 0

    # 段内引用正常命中：seq1 的 "ab"
    body = migrate(client, snap_a["snapshot_id"], snap_b["snapshot_id"], 1, 0, 1, 2).json()
    assert body["status"] == "found"
    assert body["match"] == {
        "start_seq": 1,
        "start_offset": 0,
        "end_seq": 1,
        "end_offset": 2,
    }


def test_migrate_quote_source_range_crossing_gap_rejected(client):
    submit(client, "c1", 1, 1, "ab")
    submit(client, "c1", 3, 1, "cd")
    decide_gap(client, "c1", 2, 2)
    snap_a = make_snapshot(client, "c1", "a")
    snap_b = make_snapshot(client, "c1", "b")

    # 引用区间 1..3 覆盖缺口序号 2：引用不得越过缺口
    resp = migrate(client, snap_a["snapshot_id"], snap_b["snapshot_id"], 1, 0, 3, 2)
    assert resp.status_code == 422
    assert resp.json()["error"] == "quote_crosses_gap"


# ---------- 序号 / 偏移校验 ----------


def test_migrate_quote_seq_out_of_snapshot(client):
    submit(client, "c1", 1, 1, "ab")
    snap_a = make_snapshot(client, "c1", "a")
    submit(client, "c1", 2, 1, "cd")
    snap_b = make_snapshot(client, "c1", "b")

    # end_seq 超出源快照水位线（即使目标快照已有该序号）
    resp = migrate(client, snap_a["snapshot_id"], snap_b["snapshot_id"], 1, 0, 2, 1)
    assert resp.status_code == 422
    assert resp.json()["error"] == "quote_seq_out_of_snapshot"

    # 空源快照（watermark=0）任何序号都越界
    empty = make_snapshot(client, "c2", "e")
    submit(client, "c2", 1, 1, "xy")
    snap_c2 = make_snapshot(client, "c2", "f")
    resp = migrate(client, empty["snapshot_id"], snap_c2["snapshot_id"], 1, 0, 1, 1)
    assert resp.status_code == 422
    assert resp.json()["error"] == "quote_seq_out_of_snapshot"


def test_migrate_quote_offset_out_of_range(client):
    submit(client, "c1", 1, 1, "abc")
    submit(client, "c1", 2, 1, "de")
    snap_a = make_snapshot(client, "c1", "a")
    snap_b = make_snapshot(client, "c1", "b")

    # start_offset 超过起点字幕长度
    resp = migrate(client, snap_a["snapshot_id"], snap_b["snapshot_id"], 1, 4, 2, 1)
    assert resp.status_code == 422
    assert resp.json()["error"] == "quote_offset_out_of_range"

    # end_offset 超过终点字幕长度
    resp = migrate(client, snap_a["snapshot_id"], snap_b["snapshot_id"], 1, 0, 2, 3)
    assert resp.status_code == 422
    assert resp.json()["error"] == "quote_offset_out_of_range"

    # 边界：偏移恰好等于字幕长度合法（终点不含）
    resp = migrate(client, snap_a["snapshot_id"], snap_b["snapshot_id"], 1, 0, 1, 3)
    assert resp.status_code == 200
    assert resp.json()["quote"] == "abc"


def test_migrate_quote_empty_quote_rejected(client):
    submit(client, "c1", 1, 1, "abc")
    submit(client, "c1", 2, 1, "de")
    snap_a = make_snapshot(client, "c1", "a")
    snap_b = make_snapshot(client, "c1", "b")

    # 同一字幕内起止偏移相同
    resp = migrate(client, snap_a["snapshot_id"], snap_b["snapshot_id"], 1, 1, 1, 1)
    assert resp.status_code == 422
    assert resp.json()["error"] == "empty_quote"

    # 跨字幕但区间为空：起点在 seq1 末尾、终点在 seq2 开头（不含）
    resp = migrate(client, snap_a["snapshot_id"], snap_b["snapshot_id"], 1, 3, 2, 0)
    assert resp.status_code == 422
    assert resp.json()["error"] == "empty_quote"


def test_migrate_quote_request_validation(client):
    submit(client, "c1", 1, 1, "abc")
    snap_a = make_snapshot(client, "c1", "a")
    snap_b = make_snapshot(client, "c1", "b")
    sid_a, sid_b = snap_a["snapshot_id"], snap_b["snapshot_id"]

    # start_seq > end_seq
    assert migrate(client, sid_a, sid_b, 2, 0, 1, 0).status_code == 422
    # 同一字幕内 start_offset > end_offset
    assert migrate(client, sid_a, sid_b, 1, 2, 1, 1).status_code == 422
    # 序号为 0、负偏移
    assert migrate(client, sid_a, sid_b, 0, 0, 1, 1).status_code == 422
    assert migrate(client, sid_a, sid_b, 1, -1, 1, 1).status_code == 422
    # 缺 target_snapshot_id
    resp = client.post(
        f"/snapshots/{sid_a}/quotes/migrate",
        json={"start_seq": 1, "start_offset": 0, "end_seq": 1, "end_offset": 1},
    )
    assert resp.status_code == 422


# ---------- 快照存在性与频道 ----------


def test_migrate_quote_snapshot_not_found(client):
    submit(client, "c1", 1, 1, "abc")
    snap = make_snapshot(client, "c1", "a")
    sid = snap["snapshot_id"]

    resp = migrate(client, "no-such-source", sid, 1, 0, 1, 1)
    assert resp.status_code == 404
    assert resp.json()["error"] == "snapshot_not_found"

    resp = migrate(client, sid, "no-such-target", 1, 0, 1, 1)
    assert resp.status_code == 404
    assert resp.json()["error"] == "snapshot_not_found"


def test_migrate_quote_channel_mismatch(client):
    submit(client, "c1", 1, 1, "abc")
    snap_a = make_snapshot(client, "c1", "a")
    submit(client, "c2", 1, 1, "abc")
    snap_b = make_snapshot(client, "c2", "b")

    resp = migrate(client, snap_a["snapshot_id"], snap_b["snapshot_id"], 1, 0, 1, 1)
    assert resp.status_code == 409
    assert resp.json()["error"] == "snapshot_channel_mismatch"


# ---------- 冻结生效文本与 Unicode 码点 ----------


def test_migrate_quote_uses_frozen_effective_text(client):
    submit(client, "c1", 1, 1, "原始文本")
    correct(client, "c1", 1, 0, "生效文本")  # 快照 A 冻结的是更正后文本
    snap_a = make_snapshot(client, "c1", "a")
    correct(client, "c1", 1, 1, "快照后再改")  # 不影响既有快照
    submit(client, "c1", 2, 1, "xx生效文本yy")
    snap_b = make_snapshot(client, "c1", "b")

    body = migrate(client, snap_a["snapshot_id"], snap_b["snapshot_id"], 1, 0, 1, 4).json()
    # 按快照 A 冻结的生效文本提取，而不是原始定稿或快照后的新更正
    assert body["quote"] == "生效文本"
    assert body["status"] == "found"
    assert body["match"] == {
        "start_seq": 2,
        "start_offset": 2,
        "end_seq": 2,
        "end_offset": 6,
    }


def test_migrate_quote_unicode_code_point_offsets(client):
    submit(client, "c1", 1, 1, "a🎬b🎬c")
    snap_a = make_snapshot(client, "c1", "a")
    submit(client, "c1", 2, 1, "zz🎬zz")
    snap_b = make_snapshot(client, "c1", "b")

    # "🎬" 是单个码点：目标中共 3 处（seq1 两位 + seq2 一位）-> ambiguous
    body = migrate(client, snap_a["snapshot_id"], snap_b["snapshot_id"], 1, 1, 1, 2).json()
    assert body["quote"] == "🎬"
    assert body["status"] == "ambiguous"
    assert body["match_count"] == 3

    # 跨码点引用 "🎬b🎬"：目标中只有 seq1 原位置一处
    body = migrate(client, snap_a["snapshot_id"], snap_b["snapshot_id"], 1, 1, 1, 4).json()
    assert body["quote"] == "🎬b🎬"
    assert body["status"] == "found"
    assert body["match"] == {
        "start_seq": 1,
        "start_offset": 1,
        "end_seq": 1,
        "end_offset": 4,
    }


def test_migrate_quote_no_normalization(client):
    submit(client, "c1", 1, 1, "é")  # 单一码点
    snap_a = make_snapshot(client, "c1", "a")
    correct(client, "c1", 1, 0, "é")  # 两个码点（e + 组合符）
    snap_b = make_snapshot(client, "c1", "b")

    body = migrate(client, snap_a["snapshot_id"], snap_b["snapshot_id"], 1, 0, 1, 1).json()
    assert body["quote"] == "é"
    # 逐码点精确匹配，不做归一化：分解形式不命中
    assert body["status"] == "missing"
    assert body["match_count"] == 0


# ---------- 只读：不修改快照或当前字幕 ----------


def test_migrate_quote_read_only(client):
    submit(client, "c1", 1, 1, "abcdef")
    correct(client, "c1", 1, 0, "abcDEF")
    snap_a = make_snapshot(client, "c1", "a")
    correct(client, "c1", 1, 1, "zzz")  # 目标快照中 seq1 改为其他文本
    submit(client, "c1", 2, 1, "xxabcDEFyy")
    snap_b = make_snapshot(client, "c1", "b")
    sid_a, sid_b = snap_a["snapshot_id"], snap_b["snapshot_id"]

    body = migrate(client, sid_a, sid_b, 1, 0, 1, 6).json()
    assert body["quote"] == "abcDEF"
    assert body["status"] == "found"
    assert body["match"] == {
        "start_seq": 2,
        "start_offset": 2,
        "end_seq": 2,
        "end_offset": 8,
    }

    # 两份快照内容保持不变（两个视图）
    assert client.get(f"/snapshots/{sid_a}/items").json()["items"][0]["text"] == "abcdef"
    effective_b = client.get(
        f"/snapshots/{sid_b}/items", params={"view": "effective"}
    ).json()
    assert [i["text"] for i in effective_b["items"]] == ["zzz", "xxabcDEFyy"]

    # 当前定稿与更正链不受影响
    live = client.get("/channels/c1/finalized", params={"view": "effective"}).json()
    assert live["items"][0]["text"] == "zzz"
    audit = client.get("/channels/c1/finalized/1/corrections").json()
    assert audit["current_version"] == 2

    # 重复调用结果一致（幂等的纯计算）
    assert migrate(client, sid_a, sid_b, 1, 0, 1, 6).json() == body
