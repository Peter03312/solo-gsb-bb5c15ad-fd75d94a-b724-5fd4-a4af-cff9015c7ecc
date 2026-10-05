"""业务服务层：订单、事件摄取、判定装配与归档。"""
import json
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import constants as C
from . import db
from .db import q, q1
from .engine import evaluate, parse_dt, resolve_policy, utc_now
from .errors import AppError
from .schemas import CreateOrderRequest, EventIn

WINDOW_RULE_NOTE = "迟到更正事件仅能产生新一代判定，不修改已归档结果"


# ---------------------------------------------------------------- 订单

def get_order(order_id: str) -> dict | None:
    row = q1("SELECT * FROM orders WHERE order_id = ?", (order_id,))
    return dict(row) if row else None


def require_order(order_id: str) -> dict:
    order = get_order(order_id)
    if order is None:
        raise AppError("ORDER_NOT_FOUND", f"订单不存在: {order_id}", 404)
    return order


def create_order(req: CreateOrderRequest) -> dict:
    created_utc = parse_dt(req.created_at)
    try:
        ZoneInfo(req.timezone)
    except ZoneInfoNotFoundError:
        raise AppError("TIMEZONE_INVALID", f"无法识别的时区: {req.timezone}", 422)

    cal = q1("SELECT 1 FROM calendars WHERE calendar_code = ? LIMIT 1", (req.calendar_code,))
    if cal is None:
        raise AppError(
            "CALENDAR_NOT_FOUND",
            f"节假日表不存在: {req.calendar_code}，请先初始化或使用已有日历代码",
            422,
        )

    try:
        policy = resolve_policy(created_utc)
    except LookupError:
        raise AppError(
            "POLICY_NOT_FOUND",
            f"订单创建时间 {created_utc.isoformat()} 没有任何生效政策版本",
            422,
        )

    promise_iso = parse_dt(req.promise_by).isoformat() if req.promise_by else None
    with db.tx() as conn:
        conn.execute(
            """INSERT INTO orders(order_id, created_at, timezone, calendar_code,
                                  promise_by, weight_kg, created_policy)
               VALUES (?,?,?,?,?,?,?)""",
            (req.order_id, created_utc.isoformat(), req.timezone, req.calendar_code,
             promise_iso, req.weight_kg, policy["version"]),
        )
        conn.execute(
            """INSERT INTO generations(order_id, gen_no, opened_at, archived)
               VALUES (?,1,?,0)""",
            (req.order_id, created_utc.isoformat()),
        )
    order = require_order(req.order_id)
    order["policy_version"] = policy["version"]
    return order


# ---------------------------------------------------------------- 事件摄取

def _validate_event_shape(ev: EventIn) -> None:
    is_corr = ev.correction_action is not None
    if is_corr:
        if ev.correction_action in (C.AMEND, C.CANCEL):
            if not ev.target_event_id:
                raise AppError(
                    "CORRECTION_TARGET_REQUIRED",
                    f"更正事件 {ev.event_id}（{ev.correction_action}）缺少 target_event_id",
                )
            if ev.event_type not in C.BASE_EVENT_TYPES:
                raise AppError(
                    "EVENT_TYPE_INVALID",
                    f"更正事件类型必须是基础扫描类型之一: {sorted(C.BASE_EVENT_TYPES)}",
                )
        elif ev.correction_action == C.INSERT:
            if ev.event_type not in C.INSERTABLE_EVENT_TYPES:
                raise AppError(
                    "EVENT_TYPE_INVALID",
                    f"补录事件类型仅允许: {sorted(C.INSERTABLE_EVENT_TYPES)}",
                )
    else:
        if ev.event_type not in C.BASE_EVENT_TYPES:
            raise AppError(
                "EVENT_TYPE_INVALID",
                f"未知扫描事件类型: {ev.event_type}，允许: {sorted(C.BASE_EVENT_TYPES)}",
            )


def _latest_gen(conn, order_id: str):
    return conn.execute(
        "SELECT * FROM generations WHERE order_id = ? ORDER BY gen_no DESC LIMIT 1",
        (order_id,),
    ).fetchone()


def ingest_events(order_id: str, events: list[EventIn]) -> dict:
    """按到达顺序原子写入整批事件。

    - 重放（event_id 已存在，按订单维度）跳过且不计入顺序；
    - 到达时间必须相对已接受流单调不减，否则整批回滚并返回 EVENT_ORDER_INVALID；
    - 最新一代已归档时，任一新事件开启新一代（迟到事件只产生新判定）。
    """
    require_order(order_id)
    for ev in events:
        _validate_event_shape(ev)

    items = []
    duplicates = []
    accepted = 0
    reopened = False
    current_gen_no = 1

    with db.tx() as conn:
        prev_row = conn.execute(
            "SELECT arrived_at FROM events WHERE order_id = ? ORDER BY arrived_at DESC LIMIT 1",
            (order_id,),
        ).fetchone()
        prev_arrived = parse_dt(prev_row["arrived_at"]) if prev_row else None

        for idx, ev in enumerate(events):
            dup = conn.execute(
                "SELECT 1 FROM events WHERE order_id = ? AND event_id = ?",
                (order_id, ev.event_id),
            ).fetchone()
            if dup:
                duplicates.append(ev.event_id)
                items.append({"index": idx, "event_id": ev.event_id, "duplicated": True})
                continue

            arrived = parse_dt(ev.arrived_at)
            if prev_arrived is not None and arrived < prev_arrived:
                raise AppError(
                    "EVENT_ORDER_INVALID",
                    f"第 {idx} 个事件 {ev.event_id} 到达时间 {arrived.isoformat()} "
                    f"早于上一已接受事件 {prev_arrived.isoformat()}，事件必须按到达时间顺序提交",
                    422,
                    {"index": idx, "event_id": ev.event_id,
                     "arrived_at": arrived.isoformat(),
                     "previous_arrived_at": prev_arrived.isoformat()},
                )

            gen = _latest_gen(conn, order_id)
            if gen["archived"]:
                # 迟到事件：开启新一代，归档结果保持不变
                conn.execute(
                    """INSERT INTO generations(order_id, gen_no, opened_at, archived, supersedes_gen)
                       VALUES (?,?,?,0,?)""",
                    (order_id, gen["gen_no"] + 1, arrived.isoformat(), gen["gen_no"]),
                )
                gen = _latest_gen(conn, order_id)
                reopened = True
            current_gen_no = gen["gen_no"]

            if ev.correction_action in (C.AMEND, C.CANCEL):
                target = conn.execute(
                    "SELECT event_type FROM events WHERE order_id = ? AND event_id = ? AND is_correction = 0",
                    (order_id, ev.target_event_id),
                ).fetchone()
                if target is None:
                    raise AppError(
                        "CORRECTION_TARGET_NOT_FOUND",
                        f"更正事件 {ev.event_id} 的目标事件 {ev.target_event_id} 不存在或不是基础扫描事件",
                        422,
                        {"event_id": ev.event_id, "target_event_id": ev.target_event_id},
                    )
                if ev.correction_action == C.AMEND and target["event_type"] != ev.event_type:
                    raise AppError(
                        "CORRECTION_TYPE_MISMATCH",
                        f"更正事件类型 {ev.event_type} 与目标事件类型 {target['event_type']} 不一致",
                        422,
                    )

            conn.execute(
                """INSERT INTO events(event_id, order_id, gen_id, event_type, event_time,
                                      arrived_at, is_correction, correction_action,
                                      target_event_id, note, prev_arrived_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (ev.event_id, order_id, gen["id"], ev.event_type,
                 parse_dt(ev.event_time).isoformat(), arrived.isoformat(),
                 int(ev.correction_action is not None), ev.correction_action,
                 ev.target_event_id, ev.note,
                 prev_arrived.isoformat() if prev_arrived else None),
            )
            prev_arrived = arrived
            accepted += 1
            items.append({
                "index": idx, "event_id": ev.event_id, "duplicated": False,
                "generation_no": current_gen_no,
            })

    return {
        "order_id": order_id,
        "accepted": accepted,
        "duplicates": duplicates,
        "reopened": reopened,
        "generation_no": current_gen_no,
        "items": items,
        "note": WINDOW_RULE_NOTE if reopened else "",
    }


# ---------------------------------------------------------------- 判定

def _load_eval_inputs(order_id: str):
    order = require_order(order_id)
    from .engine import load_policy
    policy = load_policy(order["created_policy"])
    return order, policy


def _events_until_gen(conn, order_id: str, gen_id: int):
    rows = conn.execute(
        """SELECT e.* FROM events e JOIN generations g ON e.gen_id = g.id
           WHERE e.order_id = ? AND g.id <= ? ORDER BY e.arrived_at""",
        (order_id, gen_id),
    ).fetchall()
    return [dict(r) for r in rows]


def live_determination(order_id: str, as_of: datetime | None = None) -> dict:
    order, policy = _load_eval_inputs(order_id)
    with db.tx() as conn:
        gen = _latest_gen(conn, order_id)
        gen_info = {"gen_no": gen["gen_no"], "supersedes_gen": gen["supersedes_gen"]}
        if gen["archived"]:
            # 最新一代已归档：当前判定即不可变快照，不重新计算
            return _archived_snapshot(gen)
        rows = _events_until_gen(conn, order_id, gen["id"])
    return evaluate(order, rows, policy, as_of=as_of, generation_info=gen_info)


def _archived_snapshot(gen_row) -> dict:
    snap = json.loads(gen_row["determination_json"])
    snap["archived"] = True
    snap["archived_at_utc"] = gen_row["archived_at"]
    return snap


def archive_determination(order_id: str, as_of: datetime | None = None) -> dict:
    order, policy = _load_eval_inputs(order_id)
    with db.tx() as conn:
        gen = _latest_gen(conn, order_id)
        if gen["archived"]:
            raise AppError(
                "ALREADY_ARCHIVED",
                f"订单 {order_id} 第 {gen['gen_no']} 代判定已归档；"
                "如需反映新到事件，请先摄取事件生成新一代判定",
                409,
            )
        gen_info = {"gen_no": gen["gen_no"], "supersedes_gen": gen["supersedes_gen"]}
        rows = _events_until_gen(conn, order_id, gen["id"])
        result = evaluate(order, rows, policy, as_of=as_of, generation_info=gen_info)
        result["archived"] = True
        stamp = (as_of or utc_now()).isoformat()
        result["archived_at_utc"] = stamp
        conn.execute(
            "UPDATE generations SET archived = 1, archived_at = ?, determination_json = ? WHERE id = ?",
            (stamp, json.dumps(result, ensure_ascii=False), gen["id"]),
        )
    return result


def determination_history(order_id: str) -> dict:
    require_order(order_id)
    rows = q(
        "SELECT gen_no, opened_at, archived, archived_at, supersedes_gen, determination_json "
        "FROM generations WHERE order_id = ? ORDER BY gen_no",
        (order_id,),
    )
    gens = []
    for r in rows:
        item = {
            "generation_no": r["gen_no"],
            "opened_at_utc": r["opened_at"],
            "archived": bool(r["archived"]),
            "archived_at_utc": r["archived_at"],
            "supersedes_generation": r["supersedes_gen"],
        }
        if r["determination_json"]:
            snap = json.loads(r["determination_json"])
            item["overall_status"] = snap["overall_status"]
            item["snapshot"] = snap
        gens.append(item)
    return {"order_id": order_id, "generations": gens}


def list_events(order_id: str) -> dict:
    require_order(order_id)
    rows = q(
        """SELECT e.event_id, e.event_type, e.event_time, e.arrived_at,
                  e.is_correction, e.correction_action, e.target_event_id, e.note, g.gen_no
           FROM events e JOIN generations g ON e.gen_id = g.id
           WHERE e.order_id = ? ORDER BY e.arrived_at""",
        (order_id,),
    )
    return {"order_id": order_id, "events": [dict(r) for r in rows]}
