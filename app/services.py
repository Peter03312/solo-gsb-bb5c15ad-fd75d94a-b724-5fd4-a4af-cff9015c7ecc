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
DRY_RUN_NOTE = "无写入预演：未写入任何事件、未开启新一代、未改变归档快照"


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


# ---------------------------------------------------------------- 无写入预演

def _sim_row(ev: EventIn, gen_id: int) -> dict:
    """把尚未写入的入参事件转换为与 events 表行同构的 dict，供 engine.materialize 使用。"""
    return {
        "event_id": ev.event_id,
        "order_id": "",
        "gen_id": gen_id,
        "event_type": ev.event_type,
        "event_time": parse_dt(ev.event_time).isoformat(),
        "arrived_at": parse_dt(ev.arrived_at).isoformat(),
        "is_correction": int(ev.correction_action is not None),
        "correction_action": ev.correction_action,
        "target_event_id": ev.target_event_id,
        "note": ev.note,
    }


def _shape_error(ev: EventIn) -> AppError | None:
    """复用摄取前的结构校验（更正目标必填、类型白名单），失败转 AppError。"""
    try:
        _validate_event_shape(ev)
    except AppError as e:
        return e
    return None


def _determination_diff(before: dict, after: dict) -> dict:
    """比较同一基准时刻下的基线判定与模拟判定，输出状态/截止/证据/依据差异。"""
    before_claims = {c["claim_type"]: c for c in before["claims"]}
    after_claims = {c["claim_type"]: c for c in after["claims"]}
    claims_diff = []
    for t in C.ALL_CLAIMS:
        b, a = before_claims.get(t), after_claims.get(t)
        if b is None or a is None:
            continue
        before_missing, after_missing = b["missing_evidence"], a["missing_evidence"]
        missing_changed = before_missing != after_missing
        basis_changed = b["basis"] != a["basis"]
        changed = (
            b["status"] != a["status"]
            or b["deadline_local"] != a["deadline_local"]
            or b["anchor_time"] != a["anchor_time"]
            or missing_changed or basis_changed
        )
        if changed:
            claims_diff.append({
                "claim_type": t,
                "claim_name": C.CLAIM_LABELS[t],
                "status": {"before": b["status"], "after": a["status"]},
                "deadline_local": {"before": b["deadline_local"], "after": a["deadline_local"]},
                "deadline_utc": {"before": b["deadline_utc"], "after": a["deadline_utc"]},
                "anchor_time": {"before": b["anchor_time"], "after": a["anchor_time"]},
                "missing_evidence": {
                    "before": before_missing,
                    "after": after_missing,
                    "added": [k for k in after_missing if k not in before_missing],
                    "removed": [k for k in before_missing if k not in after_missing],
                },
                "basis_changed": basis_changed,
                "basis": {"before": b["basis"], "after": a["basis"]},
            })
    before_missing, after_missing = before.get("missing_evidence", []), after.get("missing_evidence", [])
    before_basis, after_basis = before.get("basis", []), after.get("basis", [])
    basis_added = [line for line in after_basis if line not in before_basis]
    basis_removed = [line for line in before_basis if line not in after_basis]
    return {
        "changed": bool(claims_diff)
        or before["overall_status"] != after["overall_status"]
        or before.get("earliest_deadline_local") != after.get("earliest_deadline_local")
        or before_missing != after_missing
        or basis_added or basis_removed,
        "overall_status": {"before": before["overall_status"], "after": after["overall_status"]},
        "eligible_to_file": {"before": before["eligible_to_file"], "after": after["eligible_to_file"]},
        "earliest_deadline_local": {
            "before": before.get("earliest_deadline_local"),
            "after": after.get("earliest_deadline_local"),
        },
        "earliest_deadline_utc": {
            "before": before.get("earliest_deadline_utc"),
            "after": after.get("earliest_deadline_utc"),
        },
        "missing_evidence": {
            "before": before_missing,
            "after": after_missing,
            "added": [k for k in after_missing if k not in before_missing],
            "removed": [k for k in before_missing if k not in after_missing],
        },
        "basis": {"before": before_basis, "after": after_basis,
                  "added": basis_added, "removed": basis_removed},
        "claims": claims_diff,
    }


def dry_run_events(order_id: str, events: list[EventIn], as_of: datetime | None = None) -> dict:
    """无写入预演：在当前最新代事件视图上模拟整批摄取（amend/cancel/insert 同现有规则）。

    全程只持有只读事务，不 INSERT/UPDATE，不开新代、不改归档快照。
    - 批内任一事件违反到达顺序、目标或类型约束：整批预演失败（valid=false），
      返回首个失败位置与逐项校验结果，simulated_determination 为 None；
    - 最新代已归档时，模拟判定明确按"新一代（gen_no + 1）"结果计算，
      基线在同一 as_of 上实时重算，并随响应回传不可变归档快照以示其不被改写。
    """
    order, policy = _load_eval_inputs(order_id)
    as_of = parse_dt(as_of) if as_of else utc_now()

    # 与摄取前一致：先做纯结构校验，全部通过才进入顺序/目标模拟
    shape_errors: dict[int, AppError] = {}
    for idx, ev in enumerate(events):
        err = _shape_error(ev)
        if err is not None:
            shape_errors[idx] = err

    notes = [DRY_RUN_NOTE]

    with db.tx() as conn:  # 仅读取，绝不写入
        gen = _latest_gen(conn, order_id)
        latest_archived = bool(gen["archived"])
        latest_gen_no = gen["gen_no"]
        archived_snapshot = _archived_snapshot(gen) if latest_archived else None
        base_rows = _events_until_gen(conn, order_id, gen["id"])

        prev_row = conn.execute(
            "SELECT arrived_at FROM events WHERE order_id = ? ORDER BY arrived_at DESC LIMIT 1",
            (order_id,),
        ).fetchone()
        prev_arrived = parse_dt(prev_row["arrived_at"]) if prev_row else None

        items: list[dict] = []
        sim_rows: list[dict] = []
        # 批内已见 event_id（含已落库的），用于查重
        seen_ids = {
            r["event_id"] for r in conn.execute(
                "SELECT event_id FROM events WHERE order_id = ?", (order_id,)
            ).fetchall()
        }
        # 已存在的基础事件（含批内 insert/普通事件），供 amend/cancel 查目标
        base_types = {
            r["event_id"]: r["event_type"]
            for r in base_rows if not r["is_correction"]
        }

        valid = True
        first_failure = None
        duplicates = []
        simulated_gen_no = latest_gen_no
        would_open_generation = False

        def _reject(idx_, ev_, err_):
            """整批预演失败：记录首个失败位置（含下标、错误码与明细）。"""
            return False, {
                "index": idx_, "event_id": ev_.event_id,
                "code": err_.code, "message": err_.message, "details": err_.details,
            }

        for idx, ev in enumerate(events):
            item = {
                "index": idx,
                "event_id": ev.event_id,
                "event_type": ev.event_type,
                "correction_action": ev.correction_action,
                "target_event_id": ev.target_event_id,
            }

            if idx in shape_errors:
                e = shape_errors[idx]
                item.update(valid=False, status="rejected", error={
                    "code": e.code, "message": e.message, "details": e.details,
                })
                items.append(item)
                if valid:
                    valid, first_failure = _reject(idx, ev, e)
                continue

            if not valid:
                item.update(valid=False, status="skipped",
                            error={"code": "BATCH_ABORTED",
                                   "message": "首个失败位置之后的事件不再模拟（整批预演失败、保留现状）"})
                items.append(item)
                continue

            arrived = parse_dt(ev.arrived_at)

            if ev.event_id in seen_ids:
                duplicates.append(ev.event_id)
                item.update(valid=True, status="duplicate",
                            generation_no=simulated_gen_no)
                items.append(item)
                continue

            if prev_arrived is not None and arrived < prev_arrived:
                e = AppError(
                    "EVENT_ORDER_INVALID",
                    f"第 {idx} 个事件 {ev.event_id} 到达时间 {arrived.isoformat()} "
                    f"早于上一已接受事件 {prev_arrived.isoformat()}，事件必须按到达时间顺序提交",
                    422,
                    {"index": idx, "event_id": ev.event_id,
                     "arrived_at": arrived.isoformat(),
                     "previous_arrived_at": prev_arrived.isoformat()},
                )
                item.update(valid=False, status="rejected", error={
                    "code": e.code, "message": e.message, "details": e.details,
                })
                items.append(item)
                valid, first_failure = _reject(idx, ev, e)
                continue

            # 模拟代际：最新代已归档时，任一非重复事件都会开启新一代
            if latest_archived:
                simulated_gen_no = latest_gen_no + 1
                would_open_generation = True
            else:
                simulated_gen_no = latest_gen_no

            if ev.correction_action in (C.AMEND, C.CANCEL):
                target_type = base_types.get(ev.target_event_id)
                if target_type is None:
                    e = AppError(
                        "CORRECTION_TARGET_NOT_FOUND",
                        f"更正事件 {ev.event_id} 的目标事件 {ev.target_event_id} 不存在或不是基础扫描事件",
                        422,
                        {"event_id": ev.event_id, "target_event_id": ev.target_event_id},
                    )
                    item.update(valid=False, status="rejected", error={
                        "code": e.code, "message": e.message, "details": e.details,
                    })
                    items.append(item)
                    valid, first_failure = _reject(idx, ev, e)
                    continue
                if ev.correction_action == C.AMEND and target_type != ev.event_type:
                    e = AppError(
                        "CORRECTION_TYPE_MISMATCH",
                        f"更正事件类型 {ev.event_type} 与目标事件类型 {target_type} 不一致",
                        422,
                        {"event_id": ev.event_id, "target_event_id": ev.target_event_id,
                         "event_type": ev.event_type, "target_event_type": target_type},
                    )
                    item.update(valid=False, status="rejected", error={
                        "code": e.code, "message": e.message, "details": e.details,
                    })
                    items.append(item)
                    valid, first_failure = _reject(idx, ev, e)
                    continue

            seen_ids.add(ev.event_id)
            prev_arrived = arrived
            # 模拟行归属：新事件全部落在模拟代（不实际建代，用 gen_no 作占位 gen_id）
            sim_rows.append(_sim_row(ev, simulated_gen_no))
            if ev.correction_action != C.AMEND and ev.correction_action != C.CANCEL:
                # 普通事件 / insert 补录：成为可被后续更正定位的基础事件
                base_types[ev.event_id] = ev.event_type
            item.update(valid=True, status="accepted",
                        generation_no=simulated_gen_no)
            items.append(item)

        # --------------------------- 基线判定（同一 as_of 重算，绝不回快照冒充实时）
        baseline_gen_info = {"gen_no": latest_gen_no, "supersedes_gen": gen["supersedes_gen"]}
        baseline = evaluate(order, base_rows, policy, as_of=as_of,
                            generation_info=baseline_gen_info)
        baseline["current_latest_generation_archived"] = latest_archived
        if latest_archived:
            baseline["note"] = (
                "最新一代已归档：以下基线为在同一基准时刻对当前事件视图的实时重算，"
                "不可变归档快照见 archived_snapshot，预演不会改写它"
            )

        simulated = None
        diff = None
        if valid:
            all_rows = sorted(base_rows + sim_rows,
                              key=lambda r: parse_dt(r["arrived_at"]))
            sim_gen_info = (
                {"gen_no": simulated_gen_no, "supersedes_gen": latest_gen_no}
                if would_open_generation
                else {"gen_no": latest_gen_no, "supersedes_gen": gen["supersedes_gen"]}
            )
            simulated = evaluate(order, all_rows, policy, as_of=as_of,
                                 generation_info=sim_gen_info)
            simulated["computed_as_new_generation"] = would_open_generation
            if would_open_generation:
                simulated["note"] = (
                    f"订单最新一代（第 {latest_gen_no} 代）已归档，本预演结果按"
                    f"第 {simulated_gen_no} 代（新一代）计算，归档快照保持不变"
                )
            diff = _determination_diff(baseline, simulated)

        if would_open_generation:
            notes.append(WINDOW_RULE_NOTE)

    return {
        "order_id": order_id,
        "dry_run": True,
        "writes_performed": False,
        "as_of_utc": as_of.isoformat(),
        "valid": valid,
        "first_failure": first_failure,
        "items": items,
        "duplicates": duplicates,
        "accepted": sum(1 for i in items if i["status"] == "accepted"),
        "current_latest_generation": latest_gen_no,
        "current_latest_generation_archived": latest_archived,
        "would_open_generation": would_open_generation,
        "simulated_generation_no": simulated_gen_no if valid else None,
        "baseline_determination": baseline,
        "archived_snapshot": archived_snapshot,
        "simulated_determination": simulated,
        "diff": diff,
        "note": "；".join(notes),
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
