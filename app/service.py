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

    return summaries


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
