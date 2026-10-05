"""端到端调用示例（仅使用标准库 urllib）：

  python examples/call_api.py            # 默认 http://localhost:8000
  BASE_URL=http://host:port python examples/call_api.py

演示链路：
1. 创建 2025 版政策下的订单（承诺 2/10 18:00 送达）；
2. 提交"迟到签收"扫描事件 -> 查询判定（延迟可申请，截止按工作日跳过春节）；
3. 归档判定；
4. 重放同一事件 -> 不重复计入；倒序事件 -> 明确错误；
5. 迟到的更正事件（把签收修正到承诺时限前）-> 产生第 2 代新判定，第 1 代快照不变；
6. 查询判定历史。
"""
import json
import os
import urllib.error
import urllib.request

BASE = os.environ.get("BASE_URL", "http://localhost:8000")
ORDER_ID = "DEMO-20250205-001"


def call(method: str, path: str, payload=None, params=None):
    url = BASE + path
    if params:
        from urllib.parse import urlencode
        url += "?" + urlencode(params)
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            body = json.loads(resp.read().decode())
            print(f"[{method} {path}] -> {resp.status}")
            return body
    except urllib.error.HTTPError as e:
        print(f"[{method} {path}] -> {e.code} 错误：")
        print(json.dumps(json.loads(e.read().decode()), ensure_ascii=False, indent=2))
        return None


def show(title, body, keys=("overall_status", "eligible_to_file", "generation_no",
                            "earliest_deadline_local", "missing_evidence")):
    print(f"\n===== {title} =====")
    if body:
        print(json.dumps({k: body.get(k) for k in keys if k in body},
                         ensure_ascii=False, indent=2))


def main():
    print(f"目标服务：{BASE}")

    # 幂等：演示订单已存在则删除无接口，直接换 ID
    order_payload = {
        "order_id": ORDER_ID,
        "created_at": "2025-02-05T09:00:00+08:00",
        "timezone": "Asia/Shanghai",
        "calendar_code": "CN",
        "promise_by": "2025-02-10T18:00:00+08:00",
        "weight_kg": 2.5,
    }
    r = call("POST", "/api/v1/orders", order_payload)
    if r is None:
        print("订单创建失败，请确认服务已启动且已初始化（docker compose up 会自动初始化）")
        return
    print("适用政策版本：", r["created_policy"])

    signed_event = {
        "event_id": "EV-SIGNED-01",
        "event_type": "signed",
        "event_time": "2025-02-11T10:00:00+08:00",    # 扫描发生时间（晚于承诺时限）
        "arrived_at": "2025-02-11T10:05:00+08:00",    # 到达服务时间（顺序以此为准）
    }
    call("POST", f"/api/v1/orders/{ORDER_ID}/events", signed_event)

    det = call("GET", f"/api/v1/orders/{ORDER_ID}/determination",
               params={"as_of": "2025-02-20T12:00:00+08:00"})
    show("实时判定：延迟送达，10 个工作日截止（2/11 起算，跳过春节后为 2/25 23:59:59）", det)
    if det:
        print("\n延迟类判定依据：")
        for line in det["claims"][0]["basis"]:
            print("  -", line)

    archived = call("POST", f"/api/v1/orders/{ORDER_ID}/determination/archive",
                    params={"as_of": "2025-02-20T12:00:00+08:00"})
    show("归档第 1 代判定", archived)

    dup = call("POST", f"/api/v1/orders/{ORDER_ID}/events", signed_event)
    show("重放同一事件：accepted=0、duplicates 记录", dup,
         keys=("accepted", "duplicates", "generation_no"))

    bad = {"events": [
        {"event_id": "EV-LATE", "event_type": "lost_confirmed",
         "event_time": "2025-02-15T09:00:00+08:00",
         "arrived_at": "2025-02-09T08:00:00+08:00"},  # 早于已接受的 2/11
    ]}
    print("\n===== 倒序事件提交：期望 422 EVENT_ORDER_INVALID =====")
    call("POST", f"/api/v1/orders/{ORDER_ID}/events/batch", bad)

    correction = {
        "event_id": "EV-CORR-01",
        "event_type": "signed",
        "event_time": "2025-02-09T09:00:00+08:00",    # 修正后的签收时间（未迟到）
        "arrived_at": "2025-02-21T08:00:00+08:00",    # 晚于归档 -> 迟到更正
        "correction_action": "amend",
        "target_event_id": "EV-SIGNED-01",
        "note": "网点补传准确签收时间",
    }
    reopened = call("POST", f"/api/v1/orders/{ORDER_ID}/events", correction)
    show("迟到更正：自动开启第 2 代", reopened,
         keys=("accepted", "reopened", "generation_no", "note"))

    det2 = call("GET", f"/api/v1/orders/{ORDER_ID}/determination")
    show("第 2 代判定：延迟不再成立（not_applicable），旧归档未被改写", det2)

    hist = call("GET", f"/api/v1/orders/{ORDER_ID}/determinations")
    print("\n===== 判定历史 =====")
    if hist:
        for g in hist["generations"]:
            status = g.get("overall_status", "（未归档）")
            print(f"  第 {g['generation_no']} 代 | archived={g['archived']} | "
                  f"状态={status} | supersedes={g['supersedes_generation']}")

    print("\n示例完成。交互文档：" + BASE + "/docs")


if __name__ == "__main__":
    main()
