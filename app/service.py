"""核心业务：批次登记、过敏原传递与清洁裁决、更正重算。

裁决规则（按同一产线的先后顺序 seq 递增）：
  带入 = 上一批“实际可能含有的过敏原”
  残留 = 带入 - 本次清洁确认清除的过敏原        （只清除被确认覆盖的项目）
  本批 = 本批所用原料批号过敏原的并集
  实际 = 残留 ∪ 本批                            （上一批即使被扣留，其实际过敏原仍继续传递）
  未声明 = 实际 - 成品声明过敏原
  未声明非空 => 扣留(HOLD)；否则放行(RELEASE)
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from . import allergens as al
from .db import Store, dumps, loads
from .errors import bad_request, conflict, not_found


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _normalize_allergen_list(values, field: str) -> list[str]:
    """去重、保序；校验所有过敏原标识，非法立即报错（外层事务回滚）。"""
    if values is None:
        return []
    if not isinstance(values, list):
        raise bad_request("INVALID_ALLERGEN", f"字段 {field} 必须是过敏原代码数组")
    bad = [v for v in values if not isinstance(v, str) or not al.is_valid(v)]
    if bad:
        raise bad_request(
            "INVALID_ALLERGEN", f"无效的过敏原标识: {', '.join(map(str, bad))}"
        )
    return list(dict.fromkeys(values))


def _normalize_str_list(values, field: str) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, list) or any(not isinstance(v, str) or not v.strip() for v in values):
        raise bad_request("INVALID_PAYLOAD", f"字段 {field} 必须是非空字符串数组")
    return list(dict.fromkeys(v.strip() for v in values))


# ---------------------------------------------------------------- 裁决计算


def _build_reasons(undeclared: list[str], sources: dict[str, list[str]]) -> list[dict[str, str]]:
    if not undeclared:
        return [{"code": "DECLARED_COMPLETE", "message": "实际可能含有的过敏原均已在成品标签中声明，且清洁核验有效，准予放行。"}]
    reasons = []
    for code in undeclared:
        src = "、".join(sources.get(code, [])) or "来源未知"
        reasons.append(
            {
                "code": "UNDECLARED_ALLERGEN",
                "allergen": code,
                "message": f"实际可能含有未声明过敏原 {al.ALLERGENS[code]}({code})；来源：{src}。",
            }
        )
    reasons.append(
        {
            "code": "HOLD_POLICY",
            "message": "存在未声明过敏原，按换线放行政策扣留本批，不得放行。",
        }
    )
    return reasons


def _adjudicate_batch(b: dict[str, Any], previous_actual: list[str]) -> dict[str, Any]:
    """根据上一批实际过敏原与本批记录，计算本批裁决。"""
    material_allergens = b["material_allergens"]
    declared = b["declared"]
    cleaned = b["cleaned"]

    carried_in = list(previous_actual)
    # 清洁只能清除上一批残留在产线上的过敏原中被本次确认覆盖的项目
    residual = [a for a in carried_in if a not in cleaned]

    actual_set = set(residual) | set(material_allergens)
    actual = [a for a in al.ALLERGENS if a in actual_set]
    undeclared = [a for a in actual if a not in declared]

    sources: dict[str, list[str]] = {}
    for code in material_allergens:
        lots = "、".join(b["allergen_lots"].get(code, []))
        sources.setdefault(code, []).append(
            f"本批原料{('（批号 ' + lots + '）') if lots else ''}引入"
        )
    for code in residual:
        sources.setdefault(code, []).append("上一批残留在共用产线、本次清洁未确认清除")

    disposition = "HOLD" if undeclared else "RELEASE"
    return {
        "carried_in": carried_in,
        "residual": residual,
        "from_materials": list(material_allergens),
        "actual": actual,
        "undeclared": undeclared,
        "disposition": disposition,
        "sources": sources,
        "reasons": _build_reasons(undeclared, sources),
    }


def _load_batches(conn, line_id: str, start_seq: int) -> list[dict[str, Any]]:
    """读取某产线从 start_seq 起的批次，并附上本批原料过敏原与批号来源。"""
    rows = conn.execute(
        "SELECT * FROM batch WHERE line_id = ? AND seq >= ? ORDER BY seq",
        (line_id, start_seq),
    ).fetchall()
    result = []
    for row in rows:
        lot_nos = loads(row["material_lots"])
        lot_rows = conn.execute(
            "SELECT lot_no, allergens FROM material_lot WHERE lot_no IN (%s)"
            % ",".join("?" * len(lot_nos)),
            lot_nos,
        ).fetchall() if lot_nos else []
        lot_allergens: dict[str, list[str]] = {r["lot_no"]: loads(r["allergens"]) for r in lot_rows}

        material_allergens: list[str] = []
        allergen_lots: dict[str, list[str]] = {}
        for lot_no in lot_nos:
            for code in lot_allergens.get(lot_no, []):
                if code not in material_allergens:
                    material_allergens.append(code)
                allergen_lots.setdefault(code, [])
                if lot_no not in allergen_lots[code]:
                    allergen_lots[code].append(lot_no)
        material_allergens = [a for a in al.ALLERGENS if a in material_allergens]

        result.append(
            {
                "batch_no": row["batch_no"],
                "line_id": row["line_id"],
                "seq": row["seq"],
                "material_lots": lot_nos,
                "declared": loads(row["declared"]),
                "cleaned": loads(row["cleaned"]),
                "material_allergens": material_allergens,
                "allergen_lots": allergen_lots,
            }
        )
    return result


def _previous_actual(conn, line_id: str, start_seq: int) -> list[str]:
    """start_seq 之前最近一批的实际过敏原（不存在则为空）。"""
    row = conn.execute(
        """
        SELECT a.actual FROM adjudication a
        JOIN batch b ON b.batch_no = a.batch_no
        WHERE b.line_id = ? AND b.seq < ?
        ORDER BY b.seq DESC LIMIT 1
        """,
        (line_id, start_seq),
    ).fetchone()
    return loads(row["actual"]) if row else []


def recompute_from(
    conn, line_id: str, start_seq: int, trigger: str
) -> list[dict[str, Any]]:
    """从 start_seq 起按产线顺序重算本批及后续批次，落库新裁决版本。

    前置条件：start_seq 之前的批次裁决已是最新（更正入口负责保证）。
    返回被重算批次的裁决摘要列表。
    """
    batches = _load_batches(conn, line_id, start_seq)
    previous_actual = _previous_actual(conn, line_id, start_seq)
    summaries = []

    for b in batches:
        result = _adjudicate_batch(b, previous_actual)
        now = _now()

        row = conn.execute(
            "SELECT version FROM adjudication WHERE batch_no = ?",
            (b["batch_no"],),
        ).fetchone()
        new_version = (row["version"] if row else 0) + 1

        if row:
            conn.execute(
                """UPDATE adjudication SET version=?, actual=?, undeclared=?,
                   disposition=?, reasons=?, sources=?, decided_at=? WHERE batch_no=?""",
                (
                    new_version,
                    dumps(result["actual"]),
                    dumps(result["undeclared"]),
                    result["disposition"],
                    dumps(result["reasons"]),
                    dumps(result["sources"]),
                    now,
                    b["batch_no"],
                ),
            )
        else:
            conn.execute(
                """INSERT INTO adjudication
                   (batch_no, version, actual, undeclared, disposition, reasons, sources, decided_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    b["batch_no"],
                    new_version,
                    dumps(result["actual"]),
                    dumps(result["undeclared"]),
                    result["disposition"],
                    dumps(result["reasons"]),
                    dumps(result["sources"]),
                    now,
                ),
            )

        conn.execute(
            """INSERT INTO adjudication_version
               (batch_no, line_id, seq, version, actual, undeclared, disposition,
                reasons, sources, trigger, decided_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                b["batch_no"],
                line_id,
                b["seq"],
                new_version,
                dumps(result["actual"]),
                dumps(result["undeclared"]),
                result["disposition"],
                dumps(result["reasons"]),
                dumps(result["sources"]),
                trigger,
                now,
            ),
        )

        previous_actual = result["actual"]
        summaries.append(
            {
                "batch_no": b["batch_no"],
                "line_id": line_id,
                "seq": b["seq"],
                "version": new_version,
                "actual": result["actual"],
                "undeclared": result["undeclared"],
                "disposition": result["disposition"],
                "reasons": result["reasons"],
            }
        )

    # 产生了新裁决版本：含这些批次的已确认/待复核出货核验单立即（继续）转待复核，
    # 即使新结论仍为 RELEASE（待复核单须重新核验后才能再次确认）。
    changed_batch_nos = [s["batch_no"] for s in summaries if s["version"] > 1]
    if changed_batch_nos:
        _mark_docs_for_review(conn, changed_batch_nos, trigger, _now())

    return summaries


# ------------------------------------------------- 出货核验：待复核联动


TRIGGER_REASON_TEXT = {
    "REGISTER": "批次登记重算",
    "CORRECT_BATCH": "批次更正重算",
    "CORRECT_LOT": "原料批号更正重算",
}


def _trigger_reason(trigger: str) -> str:
    return TRIGGER_REASON_TEXT.get(trigger, trigger or "裁决重算")


def _mark_docs_for_review(conn, batch_nos: list[str], trigger: str, now: str) -> None:
    """含已产生新裁决版本批次的非草稿出货核验单 → 立即转为待复核。

    无论新结论是否仍为 RELEASE；已确认单的历次确认快照在
    shipment_doc_confirmation 中只增保留，不受影响。
    """
    if not batch_nos:
        return
    placeholders = ",".join("?" * len(batch_nos))
    docs = conn.execute(
        f"""SELECT DISTINCT d.doc_no AS doc_no
              FROM shipment_doc d
              JOIN shipment_doc_item i ON i.doc_no = d.doc_no
             WHERE i.batch_no IN ({placeholders})
               AND d.status IN ('CONFIRMED', 'REVIEW_PENDING')""",
        batch_nos,
    ).fetchall()

    for d in docs:
        # 本单内首个发生变化的批次（按提交顺序）作为整单待复核原因
        first = conn.execute(
            f"""SELECT i.batch_no AS batch_no,
                       a.version AS current_version,
                       a.disposition AS disposition
                  FROM shipment_doc_item i
                  JOIN adjudication a ON a.batch_no = i.batch_no
                 WHERE i.doc_no = ? AND i.batch_no IN ({placeholders})
                 ORDER BY i.position
                 LIMIT 1""",
            (d["doc_no"], *batch_nos),
        ).fetchone()
        reason = {"trigger": trigger, "reason": _trigger_reason(trigger)}
        if first is not None:
            reason.update(
                {
                    "batch_no": first["batch_no"],
                    "current_version": first["current_version"],
                    "current_disposition": first["disposition"],
                }
            )
        conn.execute(
            """UPDATE shipment_doc
                  SET status='REVIEW_PENDING', review_pending_at=?,
                      review_reason=?, updated_at=?
                WHERE doc_no=?""",
            (now, dumps(reason), now, d["doc_no"]),
        )


# ---------------------------------------------------------------- 序列化


def _batch_full(conn, row) -> dict[str, Any]:
    adj = conn.execute(
        "SELECT * FROM adjudication WHERE batch_no = ?", (row["batch_no"],)
    ).fetchone()
    return {
        "batch_no": row["batch_no"],
        "line_id": row["line_id"],
        "seq": row["seq"],
        "material_lots": loads(row["material_lots"]),
        "declared_allergens": loads(row["declared"]),
        "cleaned_allergens": loads(row["cleaned"]),
        "created_at": row["created_at"],
        "adjudication": _adj_payload(adj) if adj else None,
    }


def _adj_payload(adj) -> dict[str, Any]:
    return {
        "version": adj["version"],
        "actual_allergens": loads(adj["actual"]),
        "undeclared_allergens": loads(adj["undeclared"]),
        "disposition": adj["disposition"],
        "reasons": loads(adj["reasons"]),
        "sources": loads(adj["sources"]),
        "decided_at": adj["decided_at"],
    }


# ---------------------------------------------------------------- 服务入口


def _normalize_batch_refs(values) -> list[str]:
    """出货核验单批次列表：必须是非空字符串数组，且列表内不允许重复（保序）。"""
    if not isinstance(values, list) or not values:
        raise bad_request(
            "INVALID_PAYLOAD", "字段 batch_nos 必须是非空且无重复的批次号数组"
        )
    if any(not isinstance(v, str) or not v.strip() for v in values):
        raise bad_request(
            "INVALID_PAYLOAD", "字段 batch_nos 必须是非空且无重复的批次号数组"
        )
    refs = [v.strip() for v in values]
    seen: set[str] = set()
    dupes: list[str] = []
    for ref in refs:
        if ref in seen and ref not in dupes:
            dupes.append(ref)
        seen.add(ref)
    if dupes:
        raise conflict(
            "DUPLICATE_BATCH_IN_LIST", f"批次号在列表中重复: {', '.join(dupes)}"
        )
    return refs


def _snapshot_batch(conn, batch_no: str) -> dict[str, Any]:
    """读取批次当前裁决版本（核验/重新核验时逐批保存为快照）。"""
    row = conn.execute(
        """SELECT b.batch_no AS batch_no, b.line_id AS line_id, b.seq AS seq,
                  a.version AS version, a.disposition AS disposition
             FROM batch b JOIN adjudication a ON a.batch_no = b.batch_no
            WHERE b.batch_no = ?""",
        (batch_no,),
    ).fetchone()
    if row is None:
        return None
    return {
        "batch_no": row["batch_no"],
        "line_id": row["line_id"],
        "seq": row["seq"],
        "version": row["version"],
        "disposition": row["disposition"],
    }


def _replace_snapshot_items(conn, doc_no: str, batch_nos: list[str], now: str) -> None:
    """用提交的批次列表及其当前裁决重建逐批快照（首次核验 / 重新核验共用）。"""
    conn.execute("DELETE FROM shipment_doc_item WHERE doc_no = ?", (doc_no,))
    for position, batch_no in enumerate(batch_nos, start=1):
        snap = _snapshot_batch(conn, batch_no)
        if snap is None:
            raise bad_request("UNKNOWN_BATCH", f"批次未登记: {batch_no}")
        conn.execute(
            """INSERT INTO shipment_doc_item
               (doc_no, position, batch_no, snapshot_version, snapshot_disposition, verified_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                doc_no,
                position,
                snap["batch_no"],
                snap["version"],
                snap["disposition"],
                now,
            ),
        )


def _item_change_reason(conn, item, current_version: int, current_disposition: str) -> str | None:
    """快照版本落后于当前版本时的变化原因（取自新版本裁决版本记录）。"""
    if current_version <= item["snapshot_version"]:
        return None
    row = conn.execute(
        """SELECT trigger FROM adjudication_version
            WHERE batch_no = ? AND version = ?""",
        (item["batch_no"], current_version),
    ).fetchone()
    parts = [
        f"{_trigger_reason(row['trigger'])}（{row['trigger']}）" if row else "裁决重算"
    ]
    if current_disposition != item["snapshot_disposition"]:
        parts.append(
            f"结论由 {item['snapshot_disposition']} 变为 {current_disposition}"
        )
    return "，".join(parts)


def _load_doc_payload(conn, doc_row) -> dict[str, Any]:
    """组装出货核验单快照：单据状态 + 逐批（快照版本/当前版本/是否落后/变化原因）。"""
    items = conn.execute(
        """SELECT i.*, a.version AS current_version, a.disposition AS current_disposition,
                  b.line_id AS line_id, b.seq AS seq
             FROM shipment_doc_item i
             JOIN batch b ON b.batch_no = i.batch_no
             JOIN adjudication a ON a.batch_no = i.batch_no
            WHERE i.doc_no = ? ORDER BY i.position""",
        (doc_row["doc_no"],),
    ).fetchall()

    batch_items = []
    for it in items:
        stale = it["current_version"] > it["snapshot_version"]
        batch_items.append(
            {
                "position": it["position"],
                "batch_no": it["batch_no"],
                "line_id": it["line_id"],
                "seq": it["seq"],
                "snapshot_version": it["snapshot_version"],
                "snapshot_disposition": it["snapshot_disposition"],
                "current_version": it["current_version"],
                "current_disposition": it["current_disposition"],
                "stale": stale,
                "change_reason": _item_change_reason(
                    conn, it, it["current_version"], it["current_disposition"]
                ),
                "verified_at": it["verified_at"],
            }
        )

    payload = {
        "doc_no": doc_row["doc_no"],
        "status": doc_row["status"],
        "batch_count": len(batch_items),
        "created_at": doc_row["created_at"],
        "updated_at": doc_row["updated_at"],
        "verified_at": doc_row["verified_at"],
        "confirmed_at": doc_row["confirmed_at"],
        "review_pending_at": doc_row["review_pending_at"],
        "review_reason": loads(doc_row["review_reason"]) if doc_row["review_reason"] else None,
        "reverify_required": False,
        "items": batch_items,
    }
    if doc_row["status"] == "REVIEW_PENDING":
        payload["reverify_required"] = not (
            doc_row["verified_at"]
            and doc_row["review_pending_at"]
            and doc_row["verified_at"] >= doc_row["review_pending_at"]
        )
    return payload


def _doc_mismatches(conn, doc_no: str) -> list[dict[str, Any]]:
    """确认前原子校验：逐批比对快照版本与当前版本、当前结论。"""
    items = conn.execute(
        """SELECT i.*, a.version AS current_version, a.disposition AS current_disposition
             FROM shipment_doc_item i
             JOIN adjudication a ON a.batch_no = i.batch_no
            WHERE i.doc_no = ? ORDER BY i.position""",
        (doc_no,),
    ).fetchall()
    mismatches = []
    for it in items:
        stale = it["current_version"] > it["snapshot_version"]
        hold = it["current_disposition"] != "RELEASE"
        if not stale and not hold:
            continue
        if stale:
            reasons = ["STALE_SNAPSHOT"]
            if hold:
                reasons.append("HOLD_NOT_RELEASABLE")
        else:
            reasons = ["HOLD_NOT_RELEASABLE"]
        mismatches.append(
            {
                "batch_no": it["batch_no"],
                "snapshot_version": it["snapshot_version"],
                "current_version": it["current_version"],
                "snapshot_disposition": it["snapshot_disposition"],
                "current_disposition": it["current_disposition"],
                "reasons": reasons,
                "change_reason": _item_change_reason(
                    conn, it, it["current_version"], it["current_disposition"]
                ),
            }
        )
    return mismatches


class Service:
    def __init__(self, store: Store):
        self.store = store

    # ---- 参考数据 ----

    def list_allergens(self) -> list[dict[str, str]]:
        with self.store.read() as conn:
            rows = conn.execute("SELECT code, name_zh FROM allergen ORDER BY code").fetchall()
            return [{"code": r["code"], "name_zh": r["name_zh"]} for r in rows]

    def create_material_lot(self, lot_no: str, material: str, allergens: list[str]) -> dict[str, Any]:
        lot_no = (lot_no or "").strip()
        material = (material or "").strip()
        if not lot_no or not material:
            raise bad_request("INVALID_PAYLOAD", "lot_no 与 material 不能为空")
        codes = _normalize_allergen_list(allergens, "allergens")
        with self.store.write() as conn:
            exists = conn.execute(
                "SELECT 1 FROM material_lot WHERE lot_no = ?", (lot_no,)
            ).fetchone()
            if exists:
                raise conflict("DUPLICATE_MATERIAL_LOT", f"原料批号已存在: {lot_no}")
            now = _now()
            conn.execute(
                "INSERT INTO material_lot(lot_no, material, allergens, updated_at) VALUES (?, ?, ?, ?)",
                (lot_no, material, dumps(codes), now),
            )
        return {"lot_no": lot_no, "material": material, "allergens": codes}

    def get_material_lot(self, lot_no: str) -> dict[str, Any]:
        with self.store.read() as conn:
            row = conn.execute(
                "SELECT * FROM material_lot WHERE lot_no = ?", (lot_no,)
            ).fetchone()
            if not row:
                raise not_found("MATERIAL_LOT_NOT_FOUND", f"原料批号不存在: {lot_no}")
            return {
                "lot_no": row["lot_no"],
                "material": row["material"],
                "allergens": loads(row["allergens"]),
                "updated_at": row["updated_at"],
            }

    def list_material_lots(self) -> list[dict[str, Any]]:
        with self.store.read() as conn:
            rows = conn.execute("SELECT * FROM material_lot ORDER BY lot_no").fetchall()
            return [
                {
                    "lot_no": r["lot_no"],
                    "material": r["material"],
                    "allergens": loads(r["allergens"]),
                    "updated_at": r["updated_at"],
                }
                for r in rows
            ]

    def correct_material_lot(
        self, lot_no: str, material: str | None, allergens: list[str] | None
    ) -> dict[str, Any]:
        """更正原料批号档案：按产线顺序重算所有引用该批号的批次及其后续批次。"""
        with self.store.write() as conn:
            row = conn.execute(
                "SELECT * FROM material_lot WHERE lot_no = ?", (lot_no,)
            ).fetchone()
            if not row:
                raise not_found(
                    "CORRECTION_TARGET_NOT_FOUND", f"更正目标不存在，原料批号: {lot_no}"
                )

            new_material = (material or row["material"]).strip()
            old_allergens = loads(row["allergens"])
            new_allergens = (
                _normalize_allergen_list(allergens, "allergens")
                if allergens is not None
                else old_allergens
            )
            conn.execute(
                "UPDATE material_lot SET material=?, allergens=?, updated_at=? WHERE lot_no=?",
                (new_material, dumps(new_allergens), _now(), lot_no),
            )

            recomputed = []
            # 仅过敏原集合实际变化才会影响裁决；只改名称不产生新版本
            if new_allergens != old_allergens:
                # 受影响产线：引用该批号的最小 seq；每条产线各自从该点重算
                refs = conn.execute(
                    """SELECT b.line_id AS line_id, MIN(b.seq) AS start_seq
                       FROM batch_material bm JOIN batch b ON b.batch_no = bm.batch_no
                       WHERE bm.lot_no = ? GROUP BY b.line_id ORDER BY b.line_id""",
                    (lot_no,),
                ).fetchall()
                for ref in refs:
                    recomputed.extend(
                        recompute_from(conn, ref["line_id"], ref["start_seq"], "CORRECT_LOT")
                    )

        return {
            "corrected": {
                "type": "material_lot",
                "lot_no": lot_no,
                "material": new_material,
                "allergens": new_allergens,
            },
            "recomputed": recomputed,
        }

    # ---- 产线 ----

    def list_lines(self) -> list[dict[str, Any]]:
        with self.store.read() as conn:
            rows = conn.execute(
                """SELECT l.line_id,
                          COUNT(b.batch_no) AS batch_count,
                          COALESCE(MAX(b.seq), 0) AS last_seq
                   FROM line l LEFT JOIN batch b ON b.line_id = l.line_id
                   GROUP BY l.line_id ORDER BY l.line_id""",
            ).fetchall()
            return [
                {"line_id": r["line_id"], "batch_count": r["batch_count"], "last_seq": r["last_seq"]}
                for r in rows
            ]

    # ---- 批次 ----

    def register_batch(
        self,
        batch_no: str,
        line_id: str,
        material_lots: list[str],
        declared_allergens: list[str],
        cleaned_allergens: list[str],
    ) -> dict[str, Any]:
        batch_no = (batch_no or "").strip()
        line_id = (line_id or "").strip()
        if not batch_no or not line_id:
            raise bad_request("INVALID_PAYLOAD", "batch_no 与 line_id 不能为空")
        lots = _normalize_str_list(material_lots, "material_lots")
        declared = _normalize_allergen_list(declared_allergens, "declared_allergens")
        cleaned = _normalize_allergen_list(cleaned_allergens, "cleaned_allergens")

        with self.store.write() as conn:
            if conn.execute("SELECT 1 FROM batch WHERE batch_no = ?", (batch_no,)).fetchone():
                raise conflict("DUPLICATE_BATCH", f"批次号重复: {batch_no}")

            # 未知原料批号：必须先登记原料批号档案
            if lots:
                placeholders = ",".join("?" * len(lots))
                known = {
                    r["lot_no"]
                    for r in conn.execute(
                        f"SELECT lot_no FROM material_lot WHERE lot_no IN ({placeholders})",
                        lots,
                    ).fetchall()
                }
                unknown = [l for l in lots if l not in known]
                if unknown:
                    raise bad_request(
                        "UNKNOWN_MATERIAL_LOT", f"未知原料批号: {', '.join(unknown)}"
                    )

            now = _now()
            conn.execute(
                "INSERT OR IGNORE INTO line(line_id, created_at) VALUES (?, ?)",
                (line_id, now),
            )

            row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS max_seq FROM batch WHERE line_id = ?",
                (line_id,),
            ).fetchone()
            seq = row["max_seq"] + 1

            conn.execute(
                """INSERT INTO batch
                   (batch_no, line_id, seq, material_lots, declared, cleaned, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (batch_no, line_id, seq, dumps(lots), dumps(declared), dumps(cleaned), now),
            )
            conn.executemany(
                "INSERT INTO batch_material(batch_no, lot_no) VALUES (?, ?)",
                [(batch_no, lot) for lot in lots],
            )

            recompute_from(conn, line_id, seq, "REGISTER")
            full = _batch_full(
                conn,
                conn.execute("SELECT * FROM batch WHERE batch_no = ?", (batch_no,)).fetchone(),
            )
        return full

    def get_batch(self, batch_no: str) -> dict[str, Any]:
        with self.store.read() as conn:
            row = conn.execute("SELECT * FROM batch WHERE batch_no = ?", (batch_no,)).fetchone()
            if not row:
                raise not_found("BATCH_NOT_FOUND", f"批次不存在: {batch_no}")
            return _batch_full(conn, row)

    def list_batches(self, line_id: str | None = None) -> list[dict[str, Any]]:
        with self.store.read() as conn:
            if line_id:
                rows = conn.execute(
                    "SELECT * FROM batch WHERE line_id = ? ORDER BY line_id, seq",
                    (line_id,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM batch ORDER BY line_id, seq").fetchall()
            return [_batch_full(conn, r) for r in rows]

    def correct_batch(
        self,
        batch_no: str,
        material_lots: list[str] | None,
        declared_allergens: list[str] | None,
        cleaned_allergens: list[str] | None,
    ) -> dict[str, Any]:
        """更正批次的原料批号 / 声明 / 清洁核验，重算本批及后续批次。"""
        with self.store.write() as conn:
            row = conn.execute("SELECT * FROM batch WHERE batch_no = ?", (batch_no,)).fetchone()
            if not row:
                raise not_found("CORRECTION_TARGET_NOT_FOUND", f"更正目标不存在，批次号: {batch_no}")

            new_lots = (
                _normalize_str_list(material_lots, "material_lots")
                if material_lots is not None
                else loads(row["material_lots"])
            )
            new_declared = (
                _normalize_allergen_list(declared_allergens, "declared_allergens")
                if declared_allergens is not None
                else loads(row["declared"])
            )
            new_cleaned = (
                _normalize_allergen_list(cleaned_allergens, "cleaned_allergens")
                if cleaned_allergens is not None
                else loads(row["cleaned"])
            )

            if new_lots:
                placeholders = ",".join("?" * len(new_lots))
                known = {
                    r["lot_no"]
                    for r in conn.execute(
                        f"SELECT lot_no FROM material_lot WHERE lot_no IN ({placeholders})",
                        new_lots,
                    ).fetchall()
                }
                unknown = [l for l in new_lots if l not in known]
                if unknown:
                    raise bad_request("UNKNOWN_MATERIAL_LOT", f"未知原料批号: {', '.join(unknown)}")

            conn.execute(
                "UPDATE batch SET material_lots=?, declared=?, cleaned=? WHERE batch_no=?",
                (dumps(new_lots), dumps(new_declared), dumps(new_cleaned), batch_no),
            )
            conn.execute("DELETE FROM batch_material WHERE batch_no = ?", (batch_no,))
            conn.executemany(
                "INSERT INTO batch_material(batch_no, lot_no) VALUES (?, ?)",
                [(batch_no, lot) for lot in new_lots],
            )

            recomputed = recompute_from(conn, row["line_id"], row["seq"], "CORRECT_BATCH")
            full = _batch_full(
                conn,
                conn.execute("SELECT * FROM batch WHERE batch_no = ?", (batch_no,)).fetchone(),
            )

        return {"corrected": full, "recomputed": recomputed}

    def correct_cleaning(self, batch_no: str, cleaned_allergens: list[str]) -> dict[str, Any]:
        """专用入口：仅更正“批次开始前清洁核验确认清除的过敏原”。"""
        return self.correct_batch(batch_no, None, None, cleaned_allergens)

    def batch_history(self, batch_no: str) -> dict[str, Any]:
        with self.store.read() as conn:
            row = conn.execute("SELECT * FROM batch WHERE batch_no = ?", (batch_no,)).fetchone()
            if not row:
                raise not_found("BATCH_NOT_FOUND", f"批次不存在: {batch_no}")
            versions = conn.execute(
                "SELECT * FROM adjudication_version WHERE batch_no = ? ORDER BY version",
                (batch_no,),
            ).fetchall()
            return {
                "batch_no": batch_no,
                "line_id": row["line_id"],
                "seq": row["seq"],
                "history": [
                    {
                        "version": v["version"],
                        "actual_allergens": loads(v["actual"]),
                        "undeclared_allergens": loads(v["undeclared"]),
                        "disposition": v["disposition"],
                        "reasons": loads(v["reasons"]),
                        "sources": loads(v["sources"]),
                        "trigger": v["trigger"],
                        "decided_at": v["decided_at"],
                    }
                    for v in versions
                ],
            }

    # ---- 出货核验单 ----

    def create_shipment_doc(self, doc_no: str, batch_nos: list[str]) -> dict[str, Any]:
        """提交出货核验：唯一单号 + 非空、无重复的已登记批次号列表。

        保存逐批“当前裁决版本 + 结论”快照，单据状态为 DRAFT。
        未知批次 / 重复单号 / 重复批次一律拒绝，整事务回滚不留部分单据。
        """
        doc_no = (doc_no or "").strip()
        if not doc_no:
            raise bad_request("INVALID_PAYLOAD", "doc_no 不能为空")
        batches = _normalize_batch_refs(batch_nos)

        with self.store.write() as conn:
            if conn.execute(
                "SELECT 1 FROM shipment_doc WHERE doc_no = ?", (doc_no,)
            ).fetchone():
                raise conflict("DUPLICATE_SHIPMENT_DOC", f"出货核验单号重复: {doc_no}")

            # 先校验全部批次存在，再插入单据（失败即回滚，不留下部分单据）
            placeholders = ",".join("?" * len(batches))
            known = {
                r["batch_no"]
                for r in conn.execute(
                    f"SELECT batch_no FROM batch WHERE batch_no IN ({placeholders})",
                    batches,
                ).fetchall()
            }
            unknown = [b for b in batches if b not in known]
            if unknown:
                raise bad_request(
                    "UNKNOWN_BATCH", f"批次未登记: {', '.join(unknown)}"
                )

            now = _now()
            conn.execute(
                """INSERT INTO shipment_doc
                   (doc_no, status, created_at, updated_at, verified_at,
                    confirmed_at, review_pending_at, review_reason)
                   VALUES (?, 'DRAFT', ?, ?, ?, NULL, NULL, NULL)""",
                (doc_no, now, now, now),
            )
            _replace_snapshot_items(conn, doc_no, batches, now)
            row = conn.execute(
                "SELECT * FROM shipment_doc WHERE doc_no = ?", (doc_no,)
            ).fetchone()
            payload = _load_doc_payload(conn, row)
        return payload

    def reverify_shipment_doc(self, doc_no: str) -> dict[str, Any]:
        """（重新）核验：刷新逐批快照为各批次当前裁决版本。

        仅草稿或待复核单据可核验；已确认单据不可重新核验
        （其含批次发生重算时会自动转为待复核）。
        """
        doc_no = (doc_no or "").strip()
        with self.store.write() as conn:
            doc = conn.execute(
                "SELECT * FROM shipment_doc WHERE doc_no = ?", (doc_no,)
            ).fetchone()
            if not doc:
                raise not_found(
                    "SHIPMENT_DOC_NOT_FOUND", f"出货核验单不存在: {doc_no}"
                )
            if doc["status"] == "CONFIRMED":
                raise conflict(
                    "INVALID_DOC_STATUS",
                    "已确认单据无需重新核验；仅草稿或待复核单据可重新核验",
                )

            items = conn.execute(
                "SELECT batch_no FROM shipment_doc_item WHERE doc_no = ? ORDER BY position",
                (doc_no,),
            ).fetchall()
            now = _now()
            _replace_snapshot_items(conn, doc_no, [i["batch_no"] for i in items], now)
            conn.execute(
                "UPDATE shipment_doc SET verified_at=?, updated_at=? WHERE doc_no=?",
                (now, now, doc_no),
            )
            payload = _load_doc_payload(
                conn,
                conn.execute(
                    "SELECT * FROM shipment_doc WHERE doc_no = ?", (doc_no,)
                ).fetchone(),
            )
        return payload

    def confirm_shipment_doc(self, doc_no: str) -> dict[str, Any]:
        """确认出货核验单。

        原子校验：每个逐批快照版本必须仍是当前版本，且当前结论全部为 RELEASE。
        不符则保持未确认（草稿/待复核）并返回逐批不符批次及原因；
        HOLD 批次始终不能确认；待复核单据必须先重新核验才能再次确认。
        """
        doc_no = (doc_no or "").strip()
        with self.store.write() as conn:
            doc = conn.execute(
                "SELECT * FROM shipment_doc WHERE doc_no = ?", (doc_no,)
            ).fetchone()
            if not doc:
                raise not_found(
                    "SHIPMENT_DOC_NOT_FOUND", f"出货核验单不存在: {doc_no}"
                )

            mismatches = _doc_mismatches(conn, doc_no)

            # 待复核单据：必须经重新核验刷新快照后才能再次确认
            if doc["status"] == "REVIEW_PENDING":
                needs_reverify = not (
                    doc["verified_at"]
                    and doc["review_pending_at"]
                    and doc["verified_at"] >= doc["review_pending_at"]
                )
                if needs_reverify:
                    raise conflict(
                        "REVERIFY_REQUIRED",
                        "单据处于待复核状态，须重新核验刷新逐批快照后才能再次确认",
                        {"mismatches": mismatches},
                    )

            if mismatches:
                raise conflict(
                    "VERIFICATION_MISMATCH",
                    "快照版本已过期或存在 HOLD 批次，单据保持未确认",
                    {"mismatches": mismatches},
                )

            now = _now()
            revision_row = conn.execute(
                "SELECT COALESCE(MAX(revision), 0) AS max_rev "
                "FROM shipment_doc_confirmation WHERE doc_no = ?",
                (doc_no,),
            ).fetchone()
            revision = revision_row["max_rev"] + 1

            payload = _load_doc_payload(conn, doc)
            # 只追加保留历次确认快照
            conn.execute(
                """INSERT INTO shipment_doc_confirmation
                   (doc_no, confirmed_at, revision, snapshot)
                   VALUES (?, ?, ?, ?)""",
                (doc_no, now, revision, dumps(payload)),
            )
            conn.execute(
                """UPDATE shipment_doc
                      SET status='CONFIRMED', confirmed_at=?,
                          review_pending_at=NULL, review_reason=NULL, updated_at=?
                    WHERE doc_no=?""",
                (now, now, doc_no),
            )
            result = _load_doc_payload(
                conn,
                conn.execute(
                    "SELECT * FROM shipment_doc WHERE doc_no = ?", (doc_no,)
                ).fetchone(),
            )
            result["confirmation_revision"] = revision
        return result

    def get_shipment_doc(self, doc_no: str) -> dict[str, Any]:
        doc_no = (doc_no or "").strip()
        with self.store.read() as conn:
            doc = conn.execute(
                "SELECT * FROM shipment_doc WHERE doc_no = ?", (doc_no,)
            ).fetchone()
            if not doc:
                raise not_found(
                    "SHIPMENT_DOC_NOT_FOUND", f"出货核验单不存在: {doc_no}"
                )
            return _load_doc_payload(conn, doc)

    def list_shipment_docs(self, status: str | None = None) -> dict[str, Any]:
        with self.store.read() as conn:
            if status is not None:
                status = status.strip().upper()
                if status not in {"DRAFT", "CONFIRMED", "REVIEW_PENDING"}:
                    raise bad_request(
                        "INVALID_PAYLOAD",
                        "status 必须是 DRAFT / CONFIRMED / REVIEW_PENDING 之一",
                    )
                rows = conn.execute(
                    "SELECT * FROM shipment_doc WHERE status = ? ORDER BY doc_no",
                    (status,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM shipment_doc ORDER BY doc_no"
                ).fetchall()
            docs = []
            for r in rows:
                p = _load_doc_payload(conn, r)
                docs.append(
                    {k: p[k] for k in (
                        "doc_no", "status", "batch_count", "created_at",
                        "updated_at", "verified_at", "confirmed_at",
                        "review_pending_at", "review_reason", "reverify_required",
                    )}
                )
            return {"shipment_docs": docs}

    def shipment_doc_confirmations(self, doc_no: str) -> dict[str, Any]:
        """查询出货核验单历次确认快照（只追加，全部保留）。"""
        doc_no = (doc_no or "").strip()
        with self.store.read() as conn:
            doc = conn.execute(
                "SELECT 1 FROM shipment_doc WHERE doc_no = ?", (doc_no,)
            ).fetchone()
            if not doc:
                raise not_found(
                    "SHIPMENT_DOC_NOT_FOUND", f"出货核验单不存在: {doc_no}"
                )
            rows = conn.execute(
                """SELECT revision, confirmed_at, snapshot
                     FROM shipment_doc_confirmation
                    WHERE doc_no = ? ORDER BY revision""",
                (doc_no,),
            ).fetchall()
            return {
                "doc_no": doc_no,
                "confirmations": [
                    {
                        "revision": r["revision"],
                        "confirmed_at": r["confirmed_at"],
                        "snapshot": loads(r["snapshot"]),
                    }
                    for r in rows
                ],
            }
