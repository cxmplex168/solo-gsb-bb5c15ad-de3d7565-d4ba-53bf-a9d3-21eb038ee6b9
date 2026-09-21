"""SQLite 持久层：过敏原字典 + 审查记录 + 迁移恢复点。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Set

from .analysis import missing_allergen_sources, normalize_name
from .aliases import AliasResolver
from .fingerprint import result_keys, scoped_fingerprint

DB_PATH = os.environ.get("DB_PATH") or os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "data", "app.db"))

_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS allergen_dict (
    ingredient TEXT PRIMARY KEY,
    allergens  TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ingredient_aliases (
    alias_key   TEXT PRIMARY KEY,
    alias_name  TEXT NOT NULL,
    target_key  TEXT NOT NULL,
    target_name TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reviews (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    declared   TEXT NOT NULL DEFAULT '[]',
    text_a     TEXT NOT NULL,
    text_b     TEXT,
    label_a    TEXT NOT NULL DEFAULT '现版配方',
    label_b    TEXT NOT NULL DEFAULT '新版配方',
    result     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    dict_fingerprint TEXT
);
-- 迁移恢复点：始终只保留最新的一个（id 固定为 1 的单行表），
-- 在「确认导入」/「恢复」的替换事务内写入；事务回滚则恢复点也不会留下。
CREATE TABLE IF NOT EXISTS migration_restore_point (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    created_at TEXT NOT NULL,
    reason     TEXT NOT NULL,
    dict_count   INTEGER NOT NULL,
    alias_count  INTEGER NOT NULL,
    review_count INTEGER NOT NULL,
    digest     TEXT NOT NULL,
    data       TEXT NOT NULL
);
"""


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


def init_db() -> None:
    with _lock, _connect() as conn:
        conn.executescript(_SCHEMA)
        # 旧库迁移：为升级前创建的 reviews 表补齐字典指纹列
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(reviews)")}
        if "dict_fingerprint" not in cols:
            conn.execute("ALTER TABLE reviews ADD COLUMN dict_fingerprint TEXT")


# ---------- 过敏原字典 ----------

def list_dict() -> List[dict]:
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT ingredient, allergens, updated_at FROM allergen_dict "
            "ORDER BY ingredient").fetchall()
    return [{"ingredient": r["ingredient"],
             "allergens": json.loads(r["allergens"]),
             "updated_at": r["updated_at"]} for r in rows]


def dict_lookup() -> Dict[str, Set[str]]:
    """{归一化原料名: 过敏原集合}，供解析传播使用。"""
    return {normalize_name(e["ingredient"]): set(e["allergens"])
            for e in list_dict()}


def upsert_entry(ingredient: str, allergens: List[str]) -> None:
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO allergen_dict (ingredient, allergens, updated_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(ingredient) DO UPDATE SET "
            "allergens=excluded.allergens, updated_at=excluded.updated_at",
            (ingredient, json.dumps(allergens, ensure_ascii=False), _now()))


def delete_entry(ingredient: str) -> None:
    with _lock, _connect() as conn:
        conn.execute("DELETE FROM allergen_dict WHERE ingredient = ?",
                     (ingredient,))


def bulk_import(text: str) -> int:
    """按行导入，每行格式：原料名：过敏原1，过敏原2（冒号/逗号中英文均可）。"""
    count = 0
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = re.split(r"[:：]", line, maxsplit=1)
        if len(parts) == 2:
            name, rest = parts[0].strip(), parts[1]
        else:
            # 没有冒号时，第一个空白前的词作为原料名
            tokens = line.split(None, 1)
            name, rest = tokens[0], (tokens[1] if len(tokens) > 1 else "")
        allergens = [a.strip() for a in re.split(r"[,，、;；\s]+", rest) if a.strip()]
        if name:
            upsert_entry(name, allergens)
            count += 1
    return count


# ---------- 原料别名 ----------

def list_aliases() -> List[dict]:
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT alias_name, target_name FROM ingredient_aliases "
            "ORDER BY alias_name").fetchall()
    return [{"alias": r["alias_name"], "target": r["target_name"]}
            for r in rows]


def alias_edges() -> Dict[str, str]:
    """{归一化别名: 归一化标准名} 有向图出边（已通过整批校验，理应无环）。"""
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT alias_key, target_key FROM ingredient_aliases").fetchall()
    return {r["alias_key"]: r["target_key"] for r in rows}


def alias_existing() -> Dict[str, tuple]:
    """{归一化别名: (别名展示名, 标准名展示名)}，供整批校验引用既有映射。"""
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT alias_key, alias_name, target_name FROM ingredient_aliases"
        ).fetchall()
    return {r["alias_key"]: (r["alias_name"], r["target_name"]) for r in rows}


def alias_resolver() -> AliasResolver:
    """当前别名规则的解析器（key 沿出边解析到最终标准名 key）。"""
    return AliasResolver(alias_edges())


def bulk_upsert_aliases(entries: List[tuple]) -> int:
    """整批写入已通过校验的别名（原子 upsert）。

    entries 为 ``(别名原文, 标准名原文)``；归一化后别名相同的条目只写一条。
    返回写入条数。调用方必须先用 :func:`aliases.validate_alias_batch` 完成
    整批原子校验（自环 / 任意长度环 / 同一别名多目标均拒绝）。
    """
    now = _now()
    deduped: Dict[str, tuple] = {}
    for alias, target in entries:
        deduped.setdefault(
            normalize_name(alias), (alias, target))
    with _lock, _connect() as conn:
        for key, (alias, target) in deduped.items():
            conn.execute(
                "INSERT INTO ingredient_aliases "
                "(alias_key, alias_name, target_key, target_name, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(alias_key) DO UPDATE SET "
                "alias_name=excluded.alias_name, target_key=excluded.target_key, "
                "target_name=excluded.target_name, updated_at=excluded.updated_at",
                (key, alias, normalize_name(target), target, now))
    return len(deduped)


def delete_alias(alias: str) -> None:
    with _lock, _connect() as conn:
        conn.execute("DELETE FROM ingredient_aliases WHERE alias_key = ?",
                     (normalize_name(alias),))


# ---------- 审查记录 ----------

def create_review(*, name: str, declared: List[str], text_a: str,
                  text_b: Optional[str], label_a: str, label_b: str,
                  result: dict, dict_fingerprint: Optional[str] = None) -> int:
    with _lock, _connect() as conn:
        cur = conn.execute(
            "INSERT INTO reviews (name, declared, text_a, text_b, label_a, "
            "label_b, result, created_at, dict_fingerprint) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (name, json.dumps(declared, ensure_ascii=False), text_a, text_b,
             label_a, label_b, json.dumps(result, ensure_ascii=False), _now(),
             dict_fingerprint))
        return cur.lastrowid


def update_review_result(rid: int, result: dict,
                         dict_fingerprint: str) -> None:
    """字典时效复核确认后：覆盖审查结果并更新字典指纹。"""
    with _lock, _connect() as conn:
        conn.execute(
            "UPDATE reviews SET result = ?, dict_fingerprint = ? WHERE id = ?",
            (json.dumps(result, ensure_ascii=False), dict_fingerprint, rid))


def _row_to_review(row: sqlite.Row) -> dict:
    keys = row.keys()
    return {
        "id": row["id"],
        "name": row["name"],
        "declared": json.loads(row["declared"]),
        "text_a": row["text_a"],
        "text_b": row["text_b"],
        "label_a": row["label_a"],
        "label_b": row["label_b"],
        "result": json.loads(row["result"]),
        "created_at": row["created_at"],
        "dict_fingerprint": (row["dict_fingerprint"]
                             if "dict_fingerprint" in keys else None),
    }


def _recheck_status(r: dict, dictionary: Optional[Dict[str, Set[str]]] = None,
                    resolver: Optional[AliasResolver] = None):
    """计算记录的字典时效状态。

    返回 (needs_recheck, reason)：
    - 无指纹（升级前旧记录）→ (True, "legacy")，按需复核；
    - 以记录结果中 A/B 配料树实际涉及的原料（按当前别名规则解析到最终
      标准名）对当前字典重算指纹，与保存值不一致 → (True, "stale")；
    - 一致 → (False, None)。配方未涉及的原料变化不会影响指纹；别名规则
      改变但最终标准名与其过敏原映射不变时同样不标需复核。
    """
    saved = r.get("dict_fingerprint")
    if not saved:
        return True, "legacy"
    if dictionary is None:
        dictionary = dict_lookup()
    if resolver is None:
        resolver = alias_resolver()
    current = scoped_fingerprint(
        result_keys(r.get("result") or {}), dictionary, resolver.resolve)
    if current != saved:
        return True, "stale"
    return False, None


def _backfill_missing_sources(r: dict) -> dict:
    """历史记录兼容：补齐旧版结果中缺失的遗漏来源路径（missing_sources）。

    依据记录内已持久化的配料树即时重算，无需修改库内 JSON，
    因此升级前产生的审查记录重新打开时同样可以展开来源路径。
    """
    result = r.get("result") or {}
    check = result.get("label_check")
    if not check or "missing_sources" in check:
        return r
    missing = check.get("missing", [])
    if not missing:
        check["missing_sources"] = {}
        return r
    checked_label = result.get("label_check_version", r.get("label_a"))
    version = (result.get("version_b")
               if result.get("version_b") and checked_label == r.get("label_b")
               else result.get("version_a"))
    tree = (version or {}).get("tree", [])
    check["missing_sources"] = missing_allergen_sources(tree, missing)
    return r


def get_review(rid: int) -> Optional[dict]:
    with _lock, _connect() as conn:
        row = conn.execute("SELECT * FROM reviews WHERE id = ?",
                           (rid,)).fetchone()
    if not row:
        return None
    r = _backfill_missing_sources(_row_to_review(row))
    needs, reason = _recheck_status(r)
    r["needs_recheck"] = needs
    r["recheck_reason"] = reason
    return r


def list_reviews() -> List[dict]:
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM reviews ORDER BY id DESC").fetchall()
    # 一次取出当前字典与别名规则供全部记录比对，避免逐条查询
    dictionary = dict_lookup()
    resolver = alias_resolver()
    reviews = []
    for row in rows:
        r = _row_to_review(row)
        check = r["result"].get("label_check", {})
        needs, reason = _recheck_status(r, dictionary, resolver)
        reviews.append({
            "id": r["id"], "name": r["name"], "created_at": r["created_at"],
            "has_two_versions": bool(r["text_b"]),
            "missing_count": len(check.get("missing", [])),
            "over_reported_count": len(check.get("over_reported", [])),
            "needs_recheck": needs,
            "recheck_reason": reason,
        })
    return reviews


def delete_review(rid: int) -> None:
    with _lock, _connect() as conn:
        conn.execute("DELETE FROM reviews WHERE id = ?", (rid,))


# ---------- 审查数据迁移包 ----------

def _canonical(obj) -> str:
    """规范化 JSON（对象键排序、紧凑分隔符、UTF-8 不转义非 ASCII）。

    与 :func:`app.migration.canonical_json` 同口径；在本层实现以避免
    db ↔ migration 循环导入。
    """
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def snapshot_digest(snap: dict) -> str:
    """按规范化内容计算快照（dict / aliases / reviews 三段）的 SHA-256。

    恢复点的「摘要」即据此计算；恢复时用户必须输入该摘要二次确认，
    恢复交换产生的新恢复点同样可由本函数独立验算。
    """
    payload = {"dict": snap["dict"], "aliases": snap["aliases"],
               "reviews": snap["reviews"]}
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def export_snapshot() -> dict:
    """全量读取三张表（只读），供迁移包导出使用。

    字典按原料名、别名按归一化别名（alias_key）、审查记录按 ID 排序，
    同一数据库重复读取顺序稳定。
    """
    with _lock, _connect() as conn:
        return _read_snapshot(conn)


def _read_snapshot(conn: sqlite3.Connection) -> dict:
    """在给定连接上读取三张表全量数据（调用方持锁）。"""
    d = conn.execute(
        "SELECT ingredient, allergens, updated_at FROM allergen_dict "
        "ORDER BY ingredient").fetchall()
    a = conn.execute(
        "SELECT alias_name, target_name, updated_at FROM ingredient_aliases "
        "ORDER BY alias_key").fetchall()
    r = conn.execute(
        "SELECT id, name, declared, text_a, text_b, label_a, label_b, "
        "result, created_at, dict_fingerprint FROM reviews ORDER BY id"
    ).fetchall()
    return {
        "dict": [{"ingredient": row["ingredient"],
                  "allergens": json.loads(row["allergens"]),
                  "updated_at": row["updated_at"]} for row in d],
        "aliases": [{"alias": row["alias_name"], "target": row["target_name"],
                     "updated_at": row["updated_at"]} for row in a],
        "reviews": [{"id": row["id"], "name": row["name"],
                     "declared": json.loads(row["declared"]),
                     "text_a": row["text_a"], "text_b": row["text_b"],
                     "label_a": row["label_a"], "label_b": row["label_b"],
                     "result": json.loads(row["result"]),
                     "created_at": row["created_at"],
                     "dict_fingerprint": row["dict_fingerprint"]}
                    for row in r],
    }


def _insert_snapshot(conn: sqlite3.Connection, snap: dict) -> None:
    """把快照写入已清空的三张表（调用方持锁、处于事务内）。"""
    for e in snap["dict"]:
        conn.execute(
            "INSERT INTO allergen_dict (ingredient, allergens, updated_at) "
            "VALUES (?, ?, ?)",
            (e["ingredient"],
             json.dumps(e["allergens"], ensure_ascii=False),
             e["updated_at"]))
    for e in snap["aliases"]:
        conn.execute(
            "INSERT INTO ingredient_aliases "
            "(alias_key, alias_name, target_key, target_name, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (normalize_name(e["alias"]), e["alias"],
             normalize_name(e["target"]), e["target"], e["updated_at"]))
    for r in snap["reviews"]:
        conn.execute(
            "INSERT INTO reviews (id, name, declared, text_a, text_b, "
            "label_a, label_b, result, created_at, dict_fingerprint) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (r["id"], r["name"],
             json.dumps(r["declared"], ensure_ascii=False),
             r["text_a"], r["text_b"], r["label_a"], r["label_b"],
             json.dumps(r["result"], ensure_ascii=False),
             r["created_at"], r["dict_fingerprint"]))


def _clear_data_tables(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM reviews")
    conn.execute("DELETE FROM ingredient_aliases")
    conn.execute("DELETE FROM allergen_dict")


def _save_restore_point(conn: sqlite3.Connection, snap: dict,
                        reason: str) -> dict:
    """把当前快照保存为**最新**恢复点（单行覆盖，调用方持锁、处于事务内）。"""
    digest = snapshot_digest(snap)
    created_at = _now()
    counts = {"dict": len(snap["dict"]), "aliases": len(snap["aliases"]),
              "reviews": len(snap["reviews"])}
    conn.execute(
        "INSERT INTO migration_restore_point "
        "(id, created_at, reason, dict_count, alias_count, review_count, "
        "digest, data) VALUES (1, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET created_at=excluded.created_at, "
        "reason=excluded.reason, dict_count=excluded.dict_count, "
        "alias_count=excluded.alias_count, review_count=excluded.review_count, "
        "digest=excluded.digest, data=excluded.data",
        (created_at, reason, counts["dict"], counts["aliases"],
         counts["reviews"], digest, _canonical(snap)))
    return {"created_at": created_at, "reason": reason, "counts": counts,
            "digest": digest}


def _restore_point_meta(row: sqlite3.Row) -> dict:
    return {"created_at": row["created_at"], "reason": row["reason"],
            "counts": {"dict": row["dict_count"],
                       "aliases": row["alias_count"],
                       "reviews": row["review_count"]},
            "digest": row["digest"]}


def get_restore_point() -> Optional[dict]:
    """最新恢复点元数据（无恢复数据，只读）；从未产生过恢复点时返回 None。"""
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT created_at, reason, dict_count, alias_count, "
            "review_count, digest FROM migration_restore_point WHERE id = 1"
        ).fetchone()
    return _restore_point_meta(row) if row else None


def table_counts() -> dict:
    """三张表当前行数（迁移包预检展示用，只读）。"""
    with _lock, _connect() as conn:
        return {
            "dict": conn.execute(
                "SELECT COUNT(*) AS c FROM allergen_dict").fetchone()["c"],
            "aliases": conn.execute(
                "SELECT COUNT(*) AS c FROM ingredient_aliases").fetchone()["c"],
            "reviews": conn.execute(
                "SELECT COUNT(*) AS c FROM reviews").fetchone()["c"],
        }


def replace_all(*, dict_entries: List[dict], aliases: List[dict],
                reviews: List[dict], save_point: bool = True,
                point_reason: str = "import") -> Optional[dict]:
    """在**单个 SQLite 事务**内整体替换三张表；任一步失败整体回滚。

    调用方必须先通过 :func:`app.migration.validate_package` 校验。
    记录 ID、创建 / 更新时间、字典指纹均按迁移包原样保留，因此导入后
    再导出的摘要与导入前一致。``with conn:`` 正常退出时提交、异常时
    回滚，保证「整体替换」要么全部生效、要么完全不生效。

    ``save_point=True``（确认导入默认）时，在**同一事务内、执行替换前**
    先把当前三张表保存为最新恢复点（含创建时间、三类数量与内容摘要）；
    因此若后续任一步插入失败导致回滚，恢复点也一并回滚——**替换失败
    不会产生恢复点**。成功时返回恢复点元数据，不保存时返回 None。
    """
    with _lock, _connect() as conn:
        point = None
        if save_point:
            point = _save_restore_point(
                conn, _read_snapshot(conn), point_reason)
        _clear_data_tables(conn)
        _insert_snapshot(conn, {"dict": dict_entries, "aliases": aliases,
                                "reviews": reviews})
        return point


def restore_from_point(digest: str) -> dict:
    """一键恢复到最新恢复点；恢复本身同样先保存当前数据为新恢复点。

    全程在**单个 SQLite 事务**内：读取最新恢复点 → 校验摘要（用户二次
    确认输入的 64 位摘要必须逐字符一致，且恢复点内容重算摘要必须与其
    自描述摘要一致）→ 先把**当前数据**交换保存为新的最新恢复点（
    reason=``restore``，使误恢复可再次一键撤回）→ 再用旧恢复点内容
    整体替换三张表。任一步失败整体回滚，新旧数据与恢复点均保持原状。

    返回 ``{"restored": 恢复点三类数量, "restore_point": 新恢复点元数据}``。
    """
    if not isinstance(digest, str) or not digest.strip():
        raise ValueError("请输入恢复点摘要以二次确认")
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT created_at, reason, dict_count, alias_count, "
            "review_count, digest, data FROM migration_restore_point "
            "WHERE id = 1").fetchone()
        if not row:
            raise ValueError("当前没有可恢复的恢复点")
        target_digest = row["digest"]
        if digest.strip() != target_digest:
            raise ValueError("输入的摘要与最新恢复点摘要不一致，已取消恢复")
        target = json.loads(row["data"])
        if snapshot_digest(target) != target_digest:
            raise ValueError("恢复点内容校验失败（摘要不匹配），已取消恢复")
        meta = _restore_point_meta(row)
        new_point = _save_restore_point(conn, _read_snapshot(conn), "restore")
        _clear_data_tables(conn)
        _insert_snapshot(conn, target)
        return {"restored": meta["counts"], "restore_point": new_point}
