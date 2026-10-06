"""更正事件"无写入预演"调用示例（仅标准库 urllib）：

  BASE_URL=http://host:port python examples/dry_run.py

预演接口：POST /api/v1/orders/{id}/events/dry-run?as_of=...
- 请求体与 /events/batch 完全一致（一组按到达顺序排列、尚未写入的事件）；
- 不落任何事件、不开新一代、不改归档快照（writes_performed=false）；
- 返回每项校验结果、首个失败位置、模拟后各理赔状态/截止/缺失证据/判定依据，
  以及与当前最新代视图（同一 as_of）的差异 diff；
- 批内任一事件违反到达顺序、目标或类型约束 => valid=false，整批预演失败；
- 已归档订单：模拟结果明确按新一代（gen_no+1）计算，归档快照原样回传。

本脚本演示：成功预演（amend 使延迟不成立）、失败预演（倒序整批失败、现状不变）、
归档后预演（按新一代计算，快照不被改写）三种场景。
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("BASE_URL", "http://localhost:8000")
ORDER_ID = os.environ.get("ORDER_ID", "DRYRUN-DEMO-001")


def call(method: str, path: str, payload=None, params=None):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def title(t):
    print("\n" + "=" * 8 + " " + t + " " + "=" * 8)


def main():
    print(f"目标服务：{BASE}；演示订单：{ORDER_ID}")

    # 1) 备料：建单（承诺 2/10 18:00）+ 迟到签收 2/11 10:00
    code, _ = call("POST", "/api/v1/orders", {
        "order_id": ORDER_ID,
        "created_at": "2025-02-05T09:00:00+08:00",
        "timezone": "Asia/Shanghai",
        "calendar_code": "CN",
        "promise_by": "2025-02-10T18:00:00+08:00",
        "weight_kg": 2.5,
    })
    if code not in (201,):
        print("订单已存在则换 ORDER_ID 重试；当前响应码：", code)
    call("POST", f"/api/v1/orders/{ORDER_ID}/events", {
        "event_id": f"{ORDER_ID}-signed",
        "event_type": "signed",
        "event_time": "2025-02-11T10:00:00+08:00",
        "arrived_at": "2025-02-11T10:05:00+08:00",
    })

    # 2) 成功预演：amend 把签收修正到 2/9（承诺时限之前）
    title("预演①：amend 将签收修正到承诺时限前（成功批，无写入）")
    code, body = call("POST", f"/api/v1/orders/{ORDER_ID}/events/dry-run",
                      {"events": [{
                          "event_id": f"{ORDER_ID}-corr-amend",
                          "event_type": "signed",
                          "event_time": "2025-02-09T09:00:00+08:00",
                          "arrived_at": "2025-02-21T08:00:00+08:00",
                          "correction_action": "amend",
                          "target_event_id": f"{ORDER_ID}-signed",
                          "note": "网点补传准确签收时间",
                      }]},
                      params={"as_of": "2025-02-20T12:00:00+08:00"})
    print("HTTP", code, "| valid:", body["valid"],
          "| writes_performed:", body["writes_performed"])
    print("逐项结果:", [(i["index"], i["status"], i.get("generation_no")) for i in body["items"]])
    print("整体状态:", body["diff"]["overall_status"],
          "| 可申请:", body["diff"]["eligible_to_file"])
    for c in body["diff"]["claims"]:
        print(f"  - {c['claim_name']}: {c['status']['before']} -> {c['status']['after']}；"
              f"截止 {c['deadline_local']['before']} -> {c['deadline_local']['after']}")

    # 3) 失败预演：到达顺序倒序 => 整批失败，首个失败位置 + 后续 skipped
    title("预演②：批内倒序 => valid=false，首个失败位置，现状保留")
    code, body = call("POST", f"/api/v1/orders/{ORDER_ID}/events/dry-run",
                      {"events": [
                          {"event_id": f"{ORDER_ID}-e1", "event_type": "signed",
                           "event_time": "2025-03-01T10:00:00+08:00",
                           "arrived_at": "2025-03-01T10:05:00+08:00"},
                          {"event_id": f"{ORDER_ID}-e0", "event_type": "damage_reported",
                           "event_time": "2025-02-10T09:00:00+08:00",
                           "arrived_at": "2025-02-10T09:00:00+08:00"},  # 早于上一条
                          {"event_id": f"{ORDER_ID}-e2", "event_type": "signed",
                           "event_time": "2025-03-02T10:00:00+08:00",
                           "arrived_at": "2025-03-02T10:05:00+08:00"},
                      ]})
    f = body["first_failure"]
    print("HTTP", code, "| valid:", body["valid"])
    print("首个失败：index =", f["index"], "| code =", f["code"], "| event =", f["event_id"])
    print("逐项状态:", [i["status"] for i in body["items"]])
    print("simulated_determination 为 None:", body["simulated_determination"] is None)

    # 预演不留痕：已接受事件仍只有最初的签收
    _, ev = call("GET", f"/api/v1/orders/{ORDER_ID}/events")
    print("现存事件（预演前后不变）:", [e["event_id"] for e in ev["events"]])

    # 4) 归档后预演：模拟结果必须按新一代计算，快照原样保留
    title("预演③：已归档订单 => 明确按新一代结果计算")
    call("POST", f"/api/v1/orders/{ORDER_ID}/determination/archive",
         params={"as_of": "2025-02-12T10:00:00+08:00"})
    code, body = call("POST", f"/api/v1/orders/{ORDER_ID}/events/dry-run",
                      {"events": [{
                          "event_id": f"{ORDER_ID}-corr-amend2",
                          "event_type": "signed",
                          "event_time": "2025-02-09T09:00:00+08:00",
                          "arrived_at": "2025-02-22T08:00:00+08:00",
                          "correction_action": "amend",
                          "target_event_id": f"{ORDER_ID}-signed",
                      }]},
                      params={"as_of": "2025-02-22T12:00:00+08:00"})
    print("HTTP", code, "| would_open_generation:", body["would_open_generation"],
          "| simulated_generation_no:", body["simulated_generation_no"])
    print("模拟延迟状态:", body["simulated_determination"]["claims"][0]["status"],
          "| computed_as_new_generation:",
          body["simulated_determination"]["computed_as_new_generation"])
    print("归档快照（未被改写）: gen =", body["archived_snapshot"]["generation_no"],
          "延迟 =", body["archived_snapshot"]["claims"][0]["status"])
    _, hist = call("GET", f"/api/v1/orders/{ORDER_ID}/determinations")
    print("实际代数（仍只有 1 代）:",
          [(g["generation_no"], g["archived"]) for g in hist["generations"]])

    print("\n示例完成：所有预演均未写入事件、未开新代、未改变归档快照。" + BASE + "/docs")


if __name__ == "__main__":
    main()
