"""出货核验单测试。

覆盖：
- 创建：逐批快照、未知批次/重复单号/重复批次拒绝且不留部分单据
- 草稿重新核验刷新快照
- 确认原子校验：版本过期（STALE_SNAPSHOT）、HOLD（HOLD_NOT_RELEASABLE）
- 已确认单据遇裁决重算即转待复核（即使仍 RELEASE），重新核验后才能再次确认
- 历次确认快照只追加保留；再次过期须再次重新核验
- 查询：单据状态、逐批记录版本/当前版本/变化原因、列表过滤
- 与现有批次裁决/更正/历史行为兼容
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app(db_path=str(tmp_path / "ship.db"), seed_demo=False)
    with TestClient(app) as c:
        yield c


def add_lot(client, lot_no, material="原料", allergens=None):
    resp = client.post(
        "/api/material-lots",
        json={"lot_no": lot_no, "material": material, "allergens": allergens or []},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def add_batch(client, batch_no, line_id="L1", lots=None, declared=None, cleaned=None):
    resp = client.post(
        "/api/batches",
        json={
            "batch_no": batch_no,
            "line_id": line_id,
            "material_lots": lots or [],
            "declared_allergens": declared or [],
            "cleaned_allergens": cleaned or [],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def make_doc(client, doc_no, batch_nos):
    return client.post(
        "/api/shipment-docs", json={"doc_no": doc_no, "batch_nos": batch_nos}
    )


@pytest.fixture()
def release_batches(client):
    """两条均为 RELEASE 的批次（B1 引入 milk 且声明；B2 开工前清洁 milk，无原料）。"""
    add_lot(client, "RM-M", "奶粉", ["milk"])
    b1 = add_batch(client, "B1", lots=["RM-M"], declared=["milk"])
    b2 = add_batch(client, "B2", lots=[], declared=[], cleaned=["milk"])
    assert b1["adjudication"]["disposition"] == "RELEASE"
    assert b2["adjudication"]["disposition"] == "RELEASE"
    return ["B1", "B2"]


# ---------------------------------------------------------------- 创建与拒绝


def test_create_doc_saves_per_batch_snapshot(client, release_batches):
    resp = make_doc(client, "SH-1", ["B1", "B2"])
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["doc_no"] == "SH-1"
    assert body["status"] == "DRAFT"
    assert body["batch_count"] == 2
    assert body["verified_at"] is not None
    assert [i["batch_no"] for i in body["items"]] == ["B1", "B2"]
    for item in body["items"]:
        assert item["snapshot_version"] == 1
        assert item["snapshot_disposition"] == "RELEASE"
        assert item["current_version"] == 1
        assert item["current_disposition"] == "RELEASE"
        assert item["stale"] is False
        assert item["change_reason"] is None


def test_create_doc_preserves_submission_order(client, release_batches):
    resp = make_doc(client, "SH-1", ["B2", "B1"])
    positions = [(i["position"], i["batch_no"]) for i in resp.json()["items"]]
    assert positions == [(1, "B2"), (2, "B1")]


def test_unknown_batch_rejected_without_partial_doc(client, release_batches):
    resp = make_doc(client, "SH-1", ["B1", "B404"])
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "UNKNOWN_BATCH"
    # 不留下部分单据
    assert client.get("/api/shipment-docs/SH-1").status_code == 404
    assert client.get("/api/shipment-docs").json()["shipment_docs"] == []


def test_duplicate_doc_no_rejected(client, release_batches):
    assert make_doc(client, "SH-1", ["B1"]).status_code == 201
    resp = make_doc(client, "SH-1", ["B2"])
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "DUPLICATE_SHIPMENT_DOC"
    # 原单据批次列表未被改动
    items = client.get("/api/shipment-docs/SH-1").json()["items"]
    assert [i["batch_no"] for i in items] == ["B1"]


def test_duplicate_batch_in_list_rejected(client, release_batches):
    resp = make_doc(client, "SH-1", ["B1", "B1"])
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "DUPLICATE_BATCH_IN_LIST"
    assert client.get("/api/shipment-docs/SH-1").status_code == 404


def test_empty_batch_list_rejected(client):
    resp = client.post("/api/shipment-docs", json={"doc_no": "SH-1", "batch_nos": []})
    assert resp.status_code == 422


def test_blank_doc_no_rejected(client, release_batches):
    resp = client.post(
        "/api/shipment-docs", json={"doc_no": "  ", "batch_nos": ["B1"]}
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_PAYLOAD"


# ---------------------------------------------------------------- 草稿确认 / 核验


def test_confirm_all_release_succeeds(client, release_batches):
    make_doc(client, "SH-1", ["B1", "B2"])
    resp = client.post("/api/shipment-docs/SH-1/confirm")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "CONFIRMED"
    assert body["confirmed_at"] is not None
    assert body["review_pending_at"] is None
    assert body["confirmation_revision"] == 1


def test_confirm_with_hold_rejected_and_stays_draft(client, release_batches):
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    h1 = add_batch(client, "H1", lots=["RM-P"], declared=[])
    assert h1["adjudication"]["disposition"] == "HOLD"

    make_doc(client, "SH-1", ["B1", "H1"])
    resp = client.post("/api/shipment-docs/SH-1/confirm")
    assert resp.status_code == 409
    err = resp.json()["error"]
    assert err["code"] == "VERIFICATION_MISMATCH"
    (mismatch,) = err["mismatches"]
    assert mismatch["batch_no"] == "H1"
    assert mismatch["snapshot_version"] == 1
    assert mismatch["current_version"] == 1
    assert mismatch["snapshot_disposition"] == "HOLD"
    assert mismatch["current_disposition"] == "HOLD"
    assert mismatch["reasons"] == ["HOLD_NOT_RELEASABLE"]
    # 单据保持未确认
    assert client.get("/api/shipment-docs/SH-1").json()["status"] == "DRAFT"


def test_hold_batch_can_never_be_confirmed_even_after_reverify(client):
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    add_batch(client, "H1", lots=["RM-P"], declared=[])
    make_doc(client, "SH-1", ["H1"])
    # 重新核验只刷新快照，HOLD 仍是 HOLD
    client.post("/api/shipment-docs/SH-1/verify")
    resp = client.post("/api/shipment-docs/SH-1/confirm")
    assert resp.status_code == 409
    assert resp.json()["error"]["mismatches"][0]["reasons"] == ["HOLD_NOT_RELEASABLE"]
    assert client.get("/api/shipment-docs/SH-1").json()["status"] == "DRAFT"


def test_draft_reverify_refreshes_stale_snapshot(client, release_batches):
    make_doc(client, "SH-1", ["B1", "B2"])
    # 更正 B1 触发 B1/B2 重算，结论仍 RELEASE 但版本前进
    resp = client.put("/api/batches/B1", json={"declared_allergens": ["milk"]})
    assert resp.status_code == 200

    doc = client.get("/api/shipment-docs/SH-1").json()
    assert doc["status"] == "DRAFT"  # 草稿不被联动转待复核
    assert all(i["stale"] for i in doc["items"])
    assert "CORRECT_BATCH" in doc["items"][0]["change_reason"]

    # 未重新核验直接确认 → 拒绝
    resp = client.post("/api/shipment-docs/SH-1/confirm")
    assert resp.status_code == 409
    err = resp.json()["error"]
    assert err["code"] == "VERIFICATION_MISMATCH"
    assert err["mismatches"][0]["reasons"] == ["STALE_SNAPSHOT"]
    assert {m["batch_no"] for m in err["mismatches"]} == {"B1", "B2"}

    # 重新核验后快照追上当前版本，确认成功
    resp = client.post("/api/shipment-docs/SH-1/verify")
    assert resp.status_code == 200
    assert all(not i["stale"] for i in resp.json()["items"])
    assert resp.json()["items"][0]["snapshot_version"] == 2
    assert client.post("/api/shipment-docs/SH-1/confirm").status_code == 200


# ------------------------------------------------- 确认后重算 → 待复核生命周期


def test_confirmed_doc_goes_review_pending_even_if_still_release(client, release_batches):
    make_doc(client, "SH-1", ["B1", "B2"])
    assert client.post("/api/shipment-docs/SH-1/confirm").status_code == 200

    # 更正 B1（结论不变 RELEASE，仅产生新版本）→ 已确认单立即待复核
    client.put("/api/batches/B1", json={"declared_allergens": ["milk"]})
    doc = client.get("/api/shipment-docs/SH-1").json()
    assert doc["status"] == "REVIEW_PENDING"
    assert doc["review_pending_at"] is not None
    assert doc["review_reason"]["trigger"] == "CORRECT_BATCH"
    assert doc["review_reason"]["batch_no"] == "B1"
    assert doc["reverify_required"] is True
    assert all(i["stale"] for i in doc["items"])

    # 未重新核验直接确认 → REVERIFY_REQUIRED，保持待复核
    resp = client.post("/api/shipment-docs/SH-1/confirm")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "REVERIFY_REQUIRED"
    assert client.get("/api/shipment-docs/SH-1").json()["status"] == "REVIEW_PENDING"

    # 重新核验 → 快照刷新；再次确认成功
    assert client.post("/api/shipment-docs/SH-1/verify").status_code == 200
    resp = client.post("/api/shipment-docs/SH-1/confirm")
    assert resp.status_code == 200
    assert resp.json()["status"] == "CONFIRMED"
    assert resp.json()["confirmation_revision"] == 2


def test_confirmed_doc_cannot_be_reverified(client, release_batches):
    make_doc(client, "SH-1", ["B1"])
    client.post("/api/shipment-docs/SH-1/confirm")
    resp = client.post("/api/shipment-docs/SH-1/verify")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "INVALID_DOC_STATUS"


def test_review_pending_stale_again_after_reverify(client, release_batches):
    make_doc(client, "SH-1", ["B1"])
    client.post("/api/shipment-docs/SH-1/confirm")
    client.put("/api/batches/B1", json={"declared_allergens": ["milk"]})  # v2
    client.post("/api/shipment-docs/SH-1/verify")                          # 刷到 v2

    # 再次更正（v3）后，须再次重新核验
    client.put("/api/batches/B1", json={"declared_allergens": ["milk"]})
    doc = client.get("/api/shipment-docs/SH-1").json()
    assert doc["status"] == "REVIEW_PENDING"
    assert doc["reverify_required"] is True
    resp = client.post("/api/shipment-docs/SH-1/confirm")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "REVERIFY_REQUIRED"

    client.post("/api/shipment-docs/SH-1/verify")
    assert client.post("/api/shipment-docs/SH-1/confirm").status_code == 200


def test_confirmation_snapshots_are_retained(client, release_batches):
    make_doc(client, "SH-1", ["B1"])
    client.post("/api/shipment-docs/SH-1/confirm")
    client.put("/api/batches/B1", json={"declared_allergens": ["milk"]})  # v2
    client.post("/api/shipment-docs/SH-1/verify")
    client.post("/api/shipment-docs/SH-1/confirm")

    resp = client.get("/api/shipment-docs/SH-1/confirmations")
    assert resp.status_code == 200
    confs = resp.json()["confirmations"]
    assert [c["revision"] for c in confs] == [1, 2]
    assert confs[0]["snapshot"]["items"][0]["snapshot_version"] == 1
    assert confs[1]["snapshot"]["items"][0]["snapshot_version"] == 2
    assert confs[0]["confirmed_at"] != confs[1]["confirmed_at"]


def test_review_pending_with_hold_after_recompute(client):
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    # B1 放行（声明 peanut）；B2 未清洁、未声明 → 继承 peanut 残留，HOLD
    add_batch(client, "B1", lots=["RM-P"], declared=["peanut"])
    b2 = add_batch(client, "B2", lots=[], declared=[], cleaned=[])
    assert b2["adjudication"]["disposition"] == "HOLD"

    # 先以 B1 单批建单并确认
    make_doc(client, "SH-1", ["B1"])
    client.post("/api/shipment-docs/SH-1/confirm")

    # B1 更正去掉声明 peanut：B1 新版本变 HOLD；B2 本就 HOLD
    client.put("/api/batches/B1", json={"declared_allergens": []})
    doc = client.get("/api/shipment-docs/SH-1").json()
    assert doc["status"] == "REVIEW_PENDING"
    (item,) = doc["items"]
    assert item["batch_no"] == "B1"
    assert item["current_disposition"] == "HOLD"
    assert "HOLD" in item["change_reason"]

    # 重新核验后仍 HOLD → 确认被拒（HOLD 始终不能确认），保持待复核
    client.post("/api/shipment-docs/SH-1/verify")
    resp = client.post("/api/shipment-docs/SH-1/confirm")
    assert resp.status_code == 409
    err = resp.json()["error"]
    assert err["code"] == "VERIFICATION_MISMATCH"
    assert err["mismatches"][0]["reasons"] == ["HOLD_NOT_RELEASABLE"]
    assert client.get("/api/shipment-docs/SH-1").json()["status"] == "REVIEW_PENDING"

    # 恢复声明后重算为 RELEASE → 重新核验 → 可再次确认
    client.put("/api/batches/B1", json={"declared_allergens": ["peanut"]})
    client.post("/api/shipment-docs/SH-1/verify")
    assert client.post("/api/shipment-docs/SH-1/confirm").status_code == 200


def test_lot_correction_marks_confirmed_docs_only(client, release_batches):
    make_doc(client, "SH-CONF", ["B1"])
    client.post("/api/shipment-docs/SH-CONF/confirm")
    make_doc(client, "SH-DRAFT", ["B1"])  # 草稿引用同一批

    client.put("/api/material-lots/RM-M", json={"allergens": ["milk", "soy"]})
    assert client.get("/api/shipment-docs/SH-CONF").json()["status"] == "REVIEW_PENDING"
    # 草稿不联动；其逐批快照仅表现为过期
    draft = client.get("/api/shipment-docs/SH-DRAFT").json()
    assert draft["status"] == "DRAFT"
    assert draft["items"][0]["stale"] is True
    assert "CORRECT_LOT" in draft["items"][0]["change_reason"]


def test_stale_and_hold_reasons_reported_together(client):
    # RM-X 初始无过敏原：B1/B2 都 RELEASE
    add_lot(client, "RM-X", "原料X", allergens=[])
    add_batch(client, "B1", lots=["RM-X"], declared=[])
    add_batch(client, "B2", lots=[], declared=[], cleaned=[])

    make_doc(client, "SH-1", ["B1", "B2"])
    client.post("/api/shipment-docs/SH-1/confirm")

    # 原料批号更正引入未声明 sesame：B1 v2 HOLD（原料引入），B2 v2 HOLD（残留传递）
    client.put("/api/material-lots/RM-X", json={"allergens": ["sesame"]})

    # 未重新核验：待复核 + 快照既过期又 HOLD，两个原因一并返回
    resp = client.post("/api/shipment-docs/SH-1/confirm")
    assert resp.status_code == 409
    err = resp.json()["error"]
    assert err["code"] == "REVERIFY_REQUIRED"
    mismatches = {m["batch_no"]: m for m in err["mismatches"]}
    assert set(mismatches) == {"B1", "B2"}
    for batch_no, m in mismatches.items():
        assert m["snapshot_version"] == 1
        assert m["current_version"] == 2
        assert m["snapshot_disposition"] == "RELEASE"
        assert m["current_disposition"] == "HOLD"
        assert m["reasons"] == ["STALE_SNAPSHOT", "HOLD_NOT_RELEASABLE"], batch_no

    # 重新核验后快照追平，只剩 HOLD 原因，仍不能确认
    client.post("/api/shipment-docs/SH-1/verify")
    resp = client.post("/api/shipment-docs/SH-1/confirm")
    assert resp.status_code == 409
    err = resp.json()["error"]
    assert err["code"] == "VERIFICATION_MISMATCH"
    assert all(m["reasons"] == ["HOLD_NOT_RELEASABLE"] for m in err["mismatches"])


# ---------------------------------------------------------------- 查询


def test_list_and_filter_shipment_docs(client, release_batches):
    make_doc(client, "SH-1", ["B1"])
    make_doc(client, "SH-2", ["B2"])
    client.post("/api/shipment-docs/SH-1/confirm")

    docs = {d["doc_no"]: d for d in client.get("/api/shipment-docs").json()["shipment_docs"]}
    assert set(docs) == {"SH-1", "SH-2"}
    assert docs["SH-1"]["status"] == "CONFIRMED"
    assert docs["SH-2"]["status"] == "DRAFT"

    drafts = client.get("/api/shipment-docs", params={"status": "DRAFT"}).json()["shipment_docs"]
    assert [d["doc_no"] for d in drafts] == ["SH-2"]
    confirmed = client.get("/api/shipment-docs", params={"status": "CONFIRMED"}).json()["shipment_docs"]
    assert [d["doc_no"] for d in confirmed] == ["SH-1"]

    bad = client.get("/api/shipment-docs", params={"status": "NOPE"})
    assert bad.status_code == 400


def test_get_unknown_doc_returns_404(client):
    resp = client.get("/api/shipment-docs/NOPE")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "SHIPMENT_DOC_NOT_FOUND"
    assert client.post("/api/shipment-docs/NOPE/verify").status_code == 404
    assert client.post("/api/shipment-docs/NOPE/confirm").status_code == 404


def test_batch_history_and_adjudication_unchanged(client, release_batches):
    make_doc(client, "SH-1", ["B1", "B2"])
    client.post("/api/shipment-docs/SH-1/confirm")
    client.put("/api/batches/B1", json={"declared_allergens": ["milk"]})

    # 既有批次裁决/历史行为不受出货核验影响
    hist = client.get("/api/batches/B1/history").json()["history"]
    assert [v["version"] for v in hist] == [1, 2]
    assert [v["trigger"] for v in hist] == ["REGISTER", "CORRECT_BATCH"]
    cur = client.get("/api/batches/B1").json()["adjudication"]
    assert cur["version"] == 2
    assert cur["disposition"] == "RELEASE"


def test_unrelated_confirmed_doc_not_marked_review(client, release_batches):
    add_lot(client, "RM-P", "花生酱", ["peanut"])
    # 另一条产线上的独立批次（原料、产线均不相关）
    add_batch(client, "C1", line_id="L2", lots=["RM-P"], declared=["peanut"])
    make_doc(client, "SH-OTHER", ["C1"])
    client.post("/api/shipment-docs/SH-OTHER/confirm")

    # 更正 L1 上的 B1：SH-OTHER 与 C1 完全无关，保持已确认
    client.put("/api/batches/B1", json={"declared_allergens": ["milk"]})
    doc = client.get("/api/shipment-docs/SH-OTHER").json()
    assert doc["status"] == "CONFIRMED"
    assert doc["items"][0]["batch_no"] == "C1"
    assert doc["items"][0]["stale"] is False
