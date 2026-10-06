"""无写入预演（dry-run）端到端测试：

- 只读取、不落事件、不开新代、不改归档；
- 逐项校验结果与首个失败位置；
- 模拟判定（状态/截止/缺失证据/依据）及与基线的差异；
- 已归档订单必须按新一代结果计算。
"""
from tests.conftest import make_order

DRY_RUN = "/api/v1/orders/O1/events/dry-run"


def test_dry_run_valid_amend_returns_simulated_diff_without_writing(client):
    make_order(client)  # 承诺 2/10 18:00
    client.post("/api/v1/orders/O1/events", json={
        "event_id": "s1", "event_type": "signed",
        "event_time": "2025-02-11T10:00:00+08:00",
        "arrived_at": "2025-02-11T10:05:00+08:00"})
    # 归档前基线（同一 as_of）：延迟可申请
    before = client.get("/api/v1/orders/O1/determination",
                        params={"as_of": "2025-02-20T12:00:00+08:00"}).json()
    assert before["claims"][0]["status"] == "filable"

    r = client.post(DRY_RUN,
                    params={"as_of": "2025-02-20T12:00:00+08:00"},
                    json={"events": [{
                        "event_id": "c1", "event_type": "signed",
                        "event_time": "2025-02-09T09:00:00+08:00",
                        "arrived_at": "2025-02-21T08:00:00+08:00",
                        "correction_action": "amend", "target_event_id": "s1"}]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dry_run"] is True and body["writes_performed"] is False
    assert body["valid"] is True and body["first_failure"] is None
    assert body["accepted"] == 1
    assert body["items"][0]["status"] == "accepted"
    assert body["simulated_generation_no"] == 1
    assert body["would_open_generation"] is False

    sim = body["simulated_determination"]
    assert sim["claims"][0]["status"] == "not_applicable"
    assert sim["claims"][0]["deadline_local"] is None  # 不成立则无截止

    diff = body["diff"]
    assert diff["changed"] is True
    # delay 不成立；damage/loss 尚无触发事件 => 顶层为 pending
    assert diff["overall_status"] == {"before": "filable", "after": "pending"}
    assert diff["eligible_to_file"] == {"before": True, "after": False}
    delay_diff = next(c for c in diff["claims"] if c["claim_type"] == "delay")
    assert delay_diff["status"] == {"before": "filable", "after": "not_applicable"}
    assert delay_diff["basis_changed"] is True

    # 关键：什么都没有写入
    evs = client.get("/api/v1/orders/O1/events").json()["events"]
    assert [e["event_id"] for e in evs] == ["s1"]
    live = client.get("/api/v1/orders/O1/determination",
                      params={"as_of": "2025-02-20T12:00:00+08:00"}).json()
    assert live["claims"][0]["status"] == "filable"
    # 没有开新代
    gens = client.get("/api/v1/orders/O1/determinations").json()["generations"]
    assert len(gens) == 1 and gens[0]["archived"] is False


def test_dry_run_arrival_order_violation_fails_whole_batch(client):
    make_order(client)
    r = client.post(DRY_RUN, json={"events": [
        {"event_id": "e1", "event_type": "signed",
         "event_time": "2025-02-11T10:00:00+08:00",
         "arrived_at": "2025-02-11T10:05:00+08:00"},
        {"event_id": "e0", "event_type": "damage_reported",
         "event_time": "2025-02-10T09:00:00+08:00",
         "arrived_at": "2025-02-10T09:00:00+08:00"},   # 倒序
        {"event_id": "e2", "event_type": "signed",
         "event_time": "2025-02-12T10:00:00+08:00",
         "arrived_at": "2025-02-12T10:05:00+08:00"},
    ]})
    body = r.json()
    assert body["valid"] is False
    assert body["first_failure"]["code"] == "EVENT_ORDER_INVALID"
    assert body["first_failure"]["index"] == 1
    assert body["items"][0]["status"] == "accepted"
    assert body["items"][1]["status"] == "rejected"
    assert body["items"][2]["status"] == "skipped"
    assert body["simulated_determination"] is None and body["diff"] is None
    # 基线仍随响应返回，便于展示现状
    assert body["baseline_determination"]["order_id"] == "O1"
    # 现状不变
    assert client.get("/api/v1/orders/O1/events").json()["events"] == []


def test_dry_run_unknown_correction_target_rejected(client):
    make_order(client)
    r = client.post(DRY_RUN, json={"events": [{
        "event_id": "c1", "event_type": "signed",
        "event_time": "2025-02-09T09:00:00+08:00",
        "arrived_at": "2025-02-12T10:00:00+08:00",
        "correction_action": "amend", "target_event_id": "ghost"}]})
    body = r.json()
    assert body["valid"] is False
    assert body["first_failure"]["index"] == 0
    assert body["first_failure"]["code"] == "CORRECTION_TARGET_NOT_FOUND"
    assert client.get("/api/v1/orders/O1/events").json()["events"] == []


def test_dry_run_amend_type_mismatch_rejected(client):
    make_order(client)
    client.post("/api/v1/orders/O1/events", json={
        "event_id": "s1", "event_type": "signed",
        "event_time": "2025-02-11T10:00:00+08:00",
        "arrived_at": "2025-02-11T10:05:00+08:00"})
    r = client.post(DRY_RUN, json={"events": [{
        "event_id": "c1", "event_type": "damage_reported",
        "event_time": "2025-02-09T09:00:00+08:00",
        "arrived_at": "2025-02-21T10:00:00+08:00",
        "correction_action": "amend", "target_event_id": "s1"}]})
    body = r.json()
    assert body["valid"] is False
    assert body["first_failure"]["code"] == "CORRECTION_TYPE_MISMATCH"


def test_dry_run_insert_then_cancel_within_batch_and_evidence_diff(client):
    # 无任何事件：基线 delay pending / loss pending；
    # 批内先 insert 丢件确认，再 cancel 同一补录 => 模拟后仍无丢件
    make_order(client)
    r = client.post(DRY_RUN,
                    params={"as_of": "2025-02-20T12:00:00+08:00"},
                    json={"events": [
                        {"event_id": "ins1", "event_type": "lost_confirmed",
                         "event_time": "2025-02-15T09:00:00+08:00",
                         "arrived_at": "2025-02-18T08:00:00+08:00",
                         "correction_action": "insert"},
                        {"event_id": "canc1", "event_type": "lost_confirmed",
                         "event_time": "2025-02-15T09:00:00+08:00",
                         "arrived_at": "2025-02-19T08:00:00+08:00",
                         "correction_action": "cancel", "target_event_id": "ins1"},
                    ]})
    body = r.json()
    assert body["valid"] is True, body["first_failure"]
    assert [i["status"] for i in body["items"]] == ["accepted", "accepted"]
    sim = body["simulated_determination"]
    loss = next(c for c in sim["claims"] if c["claim_type"] == "loss")
    assert loss["status"] == "pending"
    assert "lost_confirmed" in loss["missing_evidence"]
    # 各理赔状态净变化为空（补录又被撤销），但顶层判定依据记录了这两条更正
    assert body["diff"]["claims"] == []
    joined = " ".join(body["diff"]["basis"]["added"])
    assert "补录" in joined and "撤销" in joined


def test_dry_run_insert_only_changes_status_deadline_and_missing_evidence(client):
    make_order(client)
    r = client.post(DRY_RUN,
                    params={"as_of": "2025-02-11T15:00:00+08:00"},
                    json={"events": [{
                        "event_id": "ins1", "event_type": "damage_reported",
                        "event_time": "2025-02-11T11:00:00+08:00",
                        "arrived_at": "2025-02-11T12:00:00+08:00",
                        "correction_action": "insert"}]})
    body = r.json()
    assert body["valid"] is True
    sim, base, diff = (body["simulated_determination"], body["baseline_determination"], body["diff"])
    before_damage = next(c for c in base["claims"] if c["claim_type"] == "damage")
    after_damage = next(c for c in sim["claims"] if c["claim_type"] == "damage")
    assert before_damage["status"] == "pending" and after_damage["status"] == "filable"
    # v2：破损 3 个工作日，2/11 起算 => 2/14 23:59:59
    assert after_damage["deadline_local"].startswith("2025-02-14T23:59:59")
    assert "damage_reported" in before_damage["missing_evidence"]
    assert "damage_reported" not in after_damage["missing_evidence"]
    dmg_diff = next(c for c in diff["claims"] if c["claim_type"] == "damage")
    assert dmg_diff["missing_evidence"]["removed"] == ["damage_reported"]
    # delay（承诺时限已过未签收）本就 filable，顶层前后均 filable，但 damage 发生变化
    assert diff["overall_status"] == {"before": "filable", "after": "filable"}
    assert diff["changed"] is True
    assert client.get("/api/v1/orders/O1/events").json()["events"] == []


def test_dry_run_after_archive_computed_as_new_generation(client):
    make_order(client)
    client.post("/api/v1/orders/O1/events", json={
        "event_id": "s1", "event_type": "signed",
        "event_time": "2025-02-11T10:00:00+08:00",
        "arrived_at": "2025-02-11T10:05:00+08:00"})
    archived = client.post("/api/v1/orders/O1/determination/archive",
                           params={"as_of": "2025-02-12T10:00:00+08:00"}).json()
    assert archived["generation_no"] == 1

    r = client.post(DRY_RUN,
                    params={"as_of": "2025-02-21T12:00:00+08:00"},
                    json={"events": [{
                        "event_id": "c1", "event_type": "signed",
                        "event_time": "2025-02-09T09:00:00+08:00",
                        "arrived_at": "2025-02-21T08:00:00+08:00",
                        "correction_action": "amend", "target_event_id": "s1"}]})
    body = r.json()
    assert body["valid"] is True
    assert body["current_latest_generation"] == 1
    assert body["current_latest_generation_archived"] is True
    assert body["would_open_generation"] is True
    assert body["simulated_generation_no"] == 2
    sim = body["simulated_determination"]
    assert sim["generation_no"] == 2
    assert sim["computed_as_new_generation"] is True
    assert sim["claims"][0]["status"] == "not_applicable"
    assert any("第 2 代判定" in b for b in sim["basis"])
    # 基线是实时重算（gen 1 视图），同时回传不可变快照
    assert body["baseline_determination"]["generation_no"] == 1
    assert body["baseline_determination"]["current_latest_generation_archived"] is True
    snap = body["archived_snapshot"]
    assert snap["archived"] is True and snap["generation_no"] == 1
    assert snap["claims"][0]["status"] == "filable"

    # 快照未被改写、没有真正开新代
    hist = client.get("/api/v1/orders/O1/determinations").json()["generations"]
    assert len(hist) == 1 and hist[0]["archived"] is True
    assert hist[0]["snapshot"]["claims"][0]["status"] == "filable"
    live = client.get("/api/v1/orders/O1/determination").json()
    assert live["archived"] is True and live["generation_no"] == 1
    # 事件仍只有 s1（c1 未落库）
    assert [e["event_id"] for e in client.get("/api/v1/orders/O1/events").json()["events"]] == ["s1"]


def test_dry_run_marks_duplicates_against_persisted_events(client):
    make_order(client)
    payload = {"event_id": "s1", "event_type": "signed",
               "event_time": "2025-02-11T10:00:00+08:00",
               "arrived_at": "2025-02-11T10:05:00+08:00"}
    client.post("/api/v1/orders/O1/events", json=payload)
    r = client.post(DRY_RUN,
                    params={"as_of": "2025-02-20T12:00:00+08:00"},
                    json={"events": [
                        payload,
                        {"event_id": "g1", "event_type": "damage_reported",
                         "event_time": "2025-02-11T11:00:00+08:00",
                         "arrived_at": "2025-02-12T10:00:00+08:00"},
                    ]})
    body = r.json()
    assert body["valid"] is True
    assert body["items"][0]["status"] == "duplicate"
    assert body["duplicates"] == ["s1"]
    assert body["accepted"] == 1
    # 重复件不参与顺序判定：第二个事件到达时间仍以已接受流 2/11 10:05 为前序
    assert body["simulated_determination"] is not None


def test_dry_run_duplicate_id_within_batch_fails_at_second_occurrence(client):
    make_order(client)
    r = client.post(DRY_RUN, json={"events": [
        {"event_id": "dup", "event_type": "signed",
         "event_time": "2025-02-11T10:00:00+08:00",
         "arrived_at": "2025-02-11T10:05:00+08:00"},
        {"event_id": "dup", "event_type": "signed",
         "event_time": "2025-02-12T10:00:00+08:00",
         "arrived_at": "2025-02-12T10:05:00+08:00"},
    ]})
    body = r.json()
    # 批内第二次出现视为重放重复（与摄取去重语义一致），整批仍然有效
    assert body["valid"] is True
    assert [i["status"] for i in body["items"]] == ["accepted", "duplicate"]
    assert body["duplicates"] == ["dup"]


def test_dry_run_shape_error_reported_per_item_with_422_on_bad_body(client):
    make_order(client)
    # amend 缺 target：schema 通过（字段可空），业务结构校验在预演结果内返回
    r = client.post(DRY_RUN, json={"events": [{
        "event_id": "c1", "event_type": "signed",
        "event_time": "2025-02-09T09:00:00+08:00",
        "arrived_at": "2025-02-12T10:00:00+08:00",
        "correction_action": "amend"}]})
    body = r.json()
    assert body["valid"] is False
    assert body["first_failure"]["code"] == "CORRECTION_TARGET_REQUIRED"

    # 请求体格式错误（naive 时间）仍然是标准 422 INVALID_REQUEST
    r2 = client.post(DRY_RUN, json={"events": [{
        "event_id": "x", "event_type": "signed",
        "event_time": "2025-02-11T10:00:00",
        "arrived_at": "2025-02-11T10:05:00+08:00"}]})
    assert r2.status_code == 422
    assert r2.json()["error"]["code"] == "INVALID_REQUEST"


def test_dry_run_unknown_order_is_404(client):
    r = client.post("/api/v1/orders/NOPE/events/dry-run", json={"events": [{
        "event_id": "e1", "event_type": "signed",
        "event_time": "2025-02-11T10:00:00+08:00",
        "arrived_at": "2025-02-11T10:05:00+08:00"}]})
    assert r.status_code == 404 and r.json()["error"]["code"] == "ORDER_NOT_FOUND"


def test_existing_ingest_endpoints_still_work_alongside_dry_run(client):
    make_order(client)
    r = client.post("/api/v1/orders/O1/events/batch", json={"events": [{
        "event_id": "s1", "event_type": "signed",
        "event_time": "2025-02-11T10:00:00+08:00",
        "arrived_at": "2025-02-11T10:05:00+08:00"}]})
    assert r.status_code == 200 and r.json()["accepted"] == 1
