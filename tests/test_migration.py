"""审查数据迁移包测试（不依赖 FastAPI，走 db + migration 全链路）。

覆盖：
- 导出：三段数据 + 格式版本 + 规范化 SHA-256；同一数据库重复导出稳定；
  过敏原 / 标签声明列表排序规范化；摘要对键顺序不敏感、对内容敏感；
- 预检（只读）：非法 JSON、顶层非对象、格式 / 版本错误、摘要不匹配、
  字段类型 / 未知字段 / 必填缺失 / 空值、字典与记录主键重复、别名
  归一化重复 / 自环 / 任意长度环、审查记录结果结构（树 / label_check /
  diff / ingredient_changes）、counts 不一致 —— 逐项返回且绝不写库；
- 令牌：预检通过签发、错误令牌拒绝、绑定摘要（换文件拒绝）、一次性；
- 导入：单事务整体替换（旧数据消失、ID / 指纹 / 时间原样保留）、
  往返导出摘要不变；事务中途失败整体回滚；
- 恢复保护：成功导入在同事务保存最新恢复点（时间 / 来源 / 三类数量 /
  摘要），替换失败不留恢复点；恢复点展示 + 一次性恢复令牌；输入摘要
  二次确认后一键恢复、恢复前数据交换为新恢复点（误恢复可再次撤回）；
  并发确认只有一次生效、并发恢复安全；空数据同样可恢复。
"""
import json
import os
import tempfile
import threading
import unittest

_DB_DIR = tempfile.mkdtemp(prefix="allergen-migration-")
os.environ["DB_PATH"] = os.path.join(_DB_DIR, "migration.db")

from app import db  # noqa: E402
from app import migration  # noqa: E402
from app.aliases import validate_alias_batch  # noqa: E402
from app.migration import (EXPORT_FORMAT, EXPORT_VERSION,  # noqa: E402
                           MigrationConfirmError, RestoreConfirmError,
                           build_export, canonical_json, confirm_import,
                           confirm_restore, package_digest, precheck_package,
                           restore_point_status, validate_package)
from app.review import analyze  # noqa: E402

DICT_TEXT = """\
小麦粉：麸质谷物
乳清粉：乳
奶粉：乳
食盐：
"""
TEXT_A = "配料表：小麦粉，巧克力酱（脱盐乳清，奶粉），食盐"


def make_review(name="迁移测试", declared=("麸质谷物",), with_fingerprint=True):
    result, fp = analyze(text_a=TEXT_A, text_b=None, label_a="现版配方",
                         label_b="新版配方", declared=list(declared))
    return db.create_review(
        name=name, declared=list(declared), text_a=TEXT_A, text_b=None,
        label_a="现版配方", label_b="新版配方", result=result,
        dict_fingerprint=fp if with_fingerprint else None)


def pkg_text(pkg) -> str:
    return json.dumps(pkg, ensure_ascii=False, indent=2, sort_keys=True)


class Base(unittest.TestCase):
    """每个用例独立数据库：4 条字典 + 1 条别名 + 2 条审查记录（1 条无指纹）。"""

    def setUp(self):
        db.DB_PATH = os.path.join(
            _DB_DIR, self.id().replace(".", "_") + ".db")
        db.init_db()
        db.bulk_import(DICT_TEXT)
        db.bulk_upsert_aliases(
            validate_alias_batch("脱盐乳清：乳清粉", db.alias_existing())["entries"])
        self.rid = make_review()
        self.legacy_rid = make_review(name="旧记录", with_fingerprint=False)


class ExportTest(Base):
    """导出结构、摘要与稳定性。"""

    def test_structure_and_digest(self):
        pkg = build_export()
        self.assertEqual(pkg["format"], EXPORT_FORMAT)
        self.assertEqual(pkg["version"], EXPORT_VERSION)
        self.assertEqual(set(pkg["data"]), {"dict", "aliases", "reviews"})
        self.assertEqual(pkg["counts"],
                         {"dict": 4, "aliases": 1, "reviews": 2})
        self.assertEqual(pkg["sha256"], package_digest(pkg))
        # 条目按主键排序
        ingredients = [e["ingredient"] for e in pkg["data"]["dict"]]
        self.assertEqual(ingredients, sorted(ingredients))
        ids = [r["id"] for r in pkg["data"]["reviews"]]
        self.assertEqual(ids, sorted(ids))

    def test_repeated_export_stable(self):
        a, b = build_export(), build_export()
        self.assertEqual(a, b)
        self.assertEqual(canonical_json(a), canonical_json(b))
        self.assertEqual(a["sha256"], b["sha256"])

    def test_lists_normalized_on_export(self):
        # 直接写库（绕过路由层排序）：乱序过敏原 / 声明在导出时排序
        db.upsert_entry("乱序原料", ["芝麻", "乳", "大豆"])
        rid = db.create_review(
            name="乱序", declared=["芝麻", "乳"], text_a="x", text_b=None,
            label_a="A", label_b="B",
            result={"version_a": {"tree": [], "allergens": [],
                                  "unknown_ingredients": []},
                    "version_b": None, "diff": None, "ingredient_changes": None,
                    "label_check": {"missing": [], "over_reported": [],
                                    "matched": []},
                    "label_check_version": "A"},
            dict_fingerprint=None)
        pkg = build_export()
        entry = next(e for e in pkg["data"]["dict"]
                     if e["ingredient"] == "乱序原料")
        self.assertEqual(entry["allergens"], ["乳", "大豆", "芝麻"])
        rev = next(r for r in pkg["data"]["reviews"] if r["id"] == rid)
        self.assertEqual(rev["declared"], ["乳", "芝麻"])

    def test_digest_canonical_regardless_of_key_order(self):
        pkg = build_export()
        reordered = {"sha256": pkg["sha256"], "counts": pkg["counts"],
                     "data": pkg["data"], "version": pkg["version"],
                     "format": pkg["format"]}
        self.assertEqual(package_digest(reordered), pkg["sha256"])
        out = precheck_package(pkg_text(reordered))
        self.assertTrue(out["ok"], out["problems"])


class PrecheckTest(Base):
    """只读预检：逐项返回问题且绝不写库。"""

    def setUp(self):
        super().setUp()
        self.pkg = build_export()
        self.text = pkg_text(self.pkg)
        self.before = db.export_snapshot()

    def tearDown(self):
        # 任何预检都不得写库
        self.assertEqual(db.export_snapshot(), self.before)

    def _tampered(self, mutate):
        bad = json.loads(self.text)
        mutate(bad)
        bad["sha256"] = package_digest(bad)  # 绕过摘要校验，专测目标校验
        return precheck_package(pkg_text(bad))

    def test_valid_package_ok_with_token_counts_and_warnings(self):
        out = precheck_package(self.text)
        self.assertTrue(out["ok"], out["problems"])
        self.assertEqual(out["problems"], [])
        self.assertTrue(out["token"])
        self.assertEqual(out["counts"], self.pkg["counts"])
        self.assertEqual(out["current_counts"], self.pkg["counts"])
        self.assertEqual(out["sha256"], self.pkg["sha256"])
        # 一条无指纹旧记录 -> 警告；当前库非空 -> 整体替换警告
        self.assertTrue(any("需复核" in w for w in out["warnings"]))
        self.assertTrue(any("整体替换" in w for w in out["warnings"]))

    def test_invalid_json(self):
        out = precheck_package("{这不是 json")
        self.assertFalse(out["ok"])
        self.assertEqual(out["problems"][0]["type"], "invalid_json")
        self.assertIsNone(out["token"])

    def test_top_level_not_object(self):
        out = precheck_package("[1, 2, 3]")
        self.assertFalse(out["ok"])
        self.assertEqual(out["problems"][0]["type"], "not_object")

    def test_format_version_and_sha_field(self):
        out = self._tampered(lambda b: b.update(format="other-pack"))
        self.assertIn("bad_format", [p["type"] for p in out["problems"]])
        out = self._tampered(lambda b: b.update(version=EXPORT_VERSION + 1))
        self.assertIn("unsupported_version", [p["type"] for p in out["problems"]])
        out = self._tampered(lambda b: b.update(version="1"))
        self.assertIn("bad_version", [p["type"] for p in out["problems"]])
        # sha256 字段本身非法（不能用重算摘要覆盖，单独构造）
        bad = json.loads(self.text)
        bad["sha256"] = "不是十六进制"
        out = precheck_package(pkg_text(bad))
        self.assertIn("bad_sha256_field", [p["type"] for p in out["problems"]])

    def test_digest_mismatch(self):
        bad = json.loads(self.text)
        bad["data"]["dict"][0]["allergens"] = ["被篡改"]
        out = precheck_package(pkg_text(bad))  # 不重算摘要
        types = [p["type"] for p in out["problems"]]
        self.assertIn("digest_mismatch", types)
        self.assertIsNone(out["token"])

    def test_field_types_unknown_fields_and_empty_value(self):
        def mutate(b):
            b["data"]["dict"] = [{"ingredient": "", "allergens": "乳",
                                  "updated_at": 1, "extra": 1}]
            b["counts"]["dict"] = 1
        out = self._tampered(mutate)
        types = [p["type"] for p in out["problems"]]
        self.assertIn("empty_value", types)
        self.assertIn("bad_field_type", types)
        self.assertIn("unknown_fields", types)
        # 逐项定位到 dict 区第 1 条
        self.assertTrue(all(p["section"] == "dict" and p["index"] == 0
                            for p in out["problems"]))

    def test_missing_fields_and_sections(self):
        out = self._tampered(lambda b: b["data"].pop("aliases"))
        self.assertIn("missing_section", [p["type"] for p in out["problems"]])

        def mutate(b):
            b["data"]["reviews"] = [{"id": 1, "name": "x"}]
            b["counts"]["reviews"] = 1
        out = self._tampered(mutate)
        missing = [p for p in out["problems"] if p["type"] == "missing_field"]
        self.assertTrue(missing)
        self.assertTrue(all(p["section"] == "reviews" and p["index"] == 0
                            for p in missing))

    def test_duplicate_primary_keys(self):
        def mutate(b):
            b["data"]["dict"].append(dict(b["data"]["dict"][0]))
            b["data"]["reviews"].append(dict(b["data"]["reviews"][0]))
        out = self._tampered(mutate)
        types = [p["type"] for p in out["problems"]]
        self.assertIn("duplicate_ingredient", types)
        self.assertIn("duplicate_id", types)

    def test_alias_graph_cycle_self_loop_and_duplicate(self):
        def mutate(b):
            b["data"]["aliases"] = [
                {"alias": "甲", "target": "乙", "updated_at": "t"},
                {"alias": "乙", "target": "甲", "updated_at": "t"},
                {"alias": "丙", "target": "丙", "updated_at": "t"},
                {"alias": "甲 ", "target": "丁", "updated_at": "t"},  # 归一化重复
            ]
            b["counts"]["aliases"] = 4
        out = self._tampered(mutate)
        types = [p["type"] for p in out["problems"]]
        self.assertIn("cycle", types)
        self.assertIn("self_loop", types)
        self.assertIn("duplicate_alias", types)
        cyc = next(p for p in out["problems"] if p["type"] == "cycle")
        self.assertIn("甲 → 乙 → 甲", cyc["message"])

    def test_review_result_structure(self):
        def mutate(b):
            b["data"]["reviews"] = [
                # 缺 result 字段
                {"id": 1, "name": "x", "declared": [], "text_a": "a",
                 "text_b": None, "label_a": "A", "label_b": "B",
                 "created_at": "t", "dict_fingerprint": None},
                # result 结构坏：label_check.missing 非数组、树节点 children 非数组
                {"id": 2, "name": "y", "declared": [], "text_a": "a",
                 "text_b": None, "label_a": "A", "label_b": "B",
                 "created_at": "t", "dict_fingerprint": "f" * 64,
                 "result": {
                     "version_a": {"tree": [{"name": "n", "own_allergens": [],
                                             "total_allergens": [],
                                             "unknown": False,
                                             "children": "坏"}],
                                   "allergens": [],
                                   "unknown_ingredients": []},
                     "version_b": None, "diff": None,
                     "ingredient_changes": None,
                     "label_check": {"missing": "乳", "over_reported": [],
                                     "matched": []},
                     "label_check_version": "A"}},
            ]
            b["counts"]["reviews"] = 2
        out = self._tampered(mutate)
        types = [p["type"] for p in out["problems"]]
        self.assertIn("missing_field", types)   # 第 1 条缺 result
        self.assertIn("bad_result", types)      # 第 2 条结构问题
        self.assertEqual({p["index"] for p in out["problems"]}, {0, 1})

    def test_counts_mismatch(self):
        # counts 不在签名载荷内：只改 counts 不会触发摘要不匹配
        out = self._tampered(lambda b: b["counts"].update(
            dict=b["counts"]["dict"] + 1))
        types = [p["type"] for p in out["problems"]]
        self.assertIn("counts_mismatch", types)
        self.assertNotIn("digest_mismatch", types)


class ImportFlowTest(Base):
    """一次性令牌与单事务整体替换。"""

    def setUp(self):
        super().setUp()
        self.pkg = build_export()
        self.text = pkg_text(self.pkg)

    def test_token_binding_and_one_time(self):
        out = precheck_package(self.text)
        token = out["token"]
        # 错误令牌拒绝
        with self.assertRaises(MigrationConfirmError):
            confirm_import(self.text, "不存在的令牌")
        # 令牌绑定摘要：换成另一个合法但不同的包 -> 拒绝
        other = json.loads(self.text)
        other["data"]["dict"] = other["data"]["dict"][:-1]
        other["counts"]["dict"] -= 1
        other["sha256"] = package_digest(other)
        with self.assertRaises(MigrationConfirmError):
            confirm_import(pkg_text(other), token)
        # 正确确认成功；令牌一次性，复用拒绝
        result = confirm_import(self.text, token)
        self.assertTrue(result["ok"])
        with self.assertRaises(MigrationConfirmError):
            confirm_import(self.text, token)

    def test_import_replaces_all_and_roundtrip_stable(self):
        tmp_rid = make_review(name="临时记录")
        original = build_export()
        # 制造差异：删别名、加字典、删记录
        db.delete_alias("脱盐乳清")
        db.upsert_entry("临时原料", ["芥末"])
        db.delete_review(tmp_rid)
        self.assertIsNone(db.get_review(tmp_rid))
        out = precheck_package(pkg_text(original))
        self.assertTrue(out["ok"], out["problems"])
        result = confirm_import(pkg_text(original), out["token"])
        self.assertEqual(result["imported"], original["counts"])
        self.assertEqual(result["sha256"], original["sha256"])
        # 替换前数据已在同事务保存为最新恢复点
        point = db.get_restore_point()
        self.assertIsNotNone(point)
        self.assertEqual(point["reason"], db.RESTORE_REASON_IMPORT)
        self.assertEqual(result["restore_point"]["summary"], point["summary"])
        self.assertEqual(point["counts"],
                         {"dict": 5, "aliases": 0, "reviews": 2})
        self.assertEqual(len(point["summary"]), 64)
        # 旧数据被整体替换：临时字典消失，别名与被删记录恢复
        self.assertNotIn("临时原料", {e["ingredient"] for e in db.list_dict()})
        self.assertEqual(len(db.list_aliases()), 1)
        self.assertIsNotNone(db.get_review(tmp_rid))
        # ID / 指纹 / 时间原样保留，时效状态不受影响
        kept = db.get_review(self.rid)
        self.assertIsNotNone(kept)
        rev = next(r for r in original["data"]["reviews"]
                   if r["id"] == self.rid)
        self.assertEqual(kept["dict_fingerprint"], rev["dict_fingerprint"])
        self.assertEqual(kept["created_at"], rev["created_at"])
        self.assertFalse(kept["needs_recheck"])
        # 往返稳定：再导出摘要与原始一致
        self.assertEqual(build_export()["sha256"], original["sha256"])

    def test_replace_all_rollback_on_failure(self):
        before = db.export_snapshot()
        bad_reviews = [{"id": 9001, "name": "坏", "declared": [],
                        "text_a": None,  # 违反 NOT NULL，事务中途失败
                        "text_b": None, "label_a": "A", "label_b": "B",
                        "result": {}, "created_at": "t",
                        "dict_fingerprint": None}]
        with self.assertRaises(Exception):
            db.replace_all(
                dict_entries=[{"ingredient": "新原料", "allergens": [],
                               "updated_at": "t"}],
                aliases=[], reviews=bad_reviews)
        # 整体回滚：DELETE 被撤销，旧数据原样保留
        self.assertEqual(db.export_snapshot(), before)
        # 恢复点在同一事务内写入：回滚后不得留下恢复点
        self.assertIsNone(db.get_restore_point())

    def test_confirm_import_failure_rolls_back_and_consumes_token(self):
        out = precheck_package(self.text)
        token = out["token"]
        before = db.export_snapshot()
        original = migration.db.replace_all

        def boom(**kwargs):
            raise RuntimeError("模拟写库失败")

        migration.db.replace_all = boom
        try:
            with self.assertRaises(RuntimeError):
                confirm_import(self.text, token)
        finally:
            migration.db.replace_all = original
        # 数据未变；失败不生成恢复点；令牌已消费（一次性）
        self.assertEqual(db.export_snapshot(), before)
        self.assertIsNone(db.get_restore_point())
        with self.assertRaises(MigrationConfirmError):
            confirm_import(self.text, token)


class RestorePointTest(Base):
    """恢复保护：导入保存恢复点、摘要二次确认、一键恢复与误恢复撤回。"""

    def setUp(self):
        super().setUp()
        # 初始数据：4 条字典 + 1 别名 + 2 审查记录；尚无恢复点
        self.original = build_export()
        self.original_text = pkg_text(self.original)
        self.assertIsNone(db.get_restore_point())

    def _do_import(self, text=None):
        text = text or self.original_text
        out = precheck_package(text)
        self.assertTrue(out["ok"], out["problems"])
        return confirm_import(text, out["token"])

    def test_status_initially_empty_and_precheck_exposes_point(self):
        status = restore_point_status()
        self.assertIsNone(status["point"])
        self.assertIsNone(status["token"])
        # 预检响应同样携带最新恢复点（此时为 None）
        out = precheck_package(self.original_text)
        self.assertTrue(out["ok"])
        self.assertIsNone(out["restore_point"])

    def test_import_creates_latest_restore_point_with_meta(self):
        # 导入前把当前库改成与迁移包不同的状态
        db.upsert_entry("临时原料", ["芥末"])
        before_snap = db.export_snapshot()
        result = self._do_import()
        point = db.get_restore_point()
        self.assertIsNotNone(point)
        self.assertEqual(point["reason"], db.RESTORE_REASON_IMPORT)
        self.assertTrue(point["created_at"])
        self.assertEqual(point["counts"],
                         {"dict": 5, "aliases": 1, "reviews": 2})
        # 摘要即替换前数据的规范化 SHA-256
        self.assertEqual(point["summary"],
                         db.snapshot_digest(before_snap))
        self.assertEqual(result["restore_point"], point)
        # 预检 / 状态接口现在都返回该恢复点
        self.assertEqual(precheck_package(self.original_text)
                         ["restore_point"]["summary"], point["summary"])
        self.assertEqual(restore_point_status()["point"], point)

    def test_successful_import_overwrites_previous_point(self):
        self._do_import()
        first = db.get_restore_point()
        # 再导入另一内容：恢复点被新的「当前数据」覆盖，只保留一份
        db.upsert_entry("再临原料", ["芝麻"])
        pre_snap = db.export_snapshot()
        self._do_import()
        second = db.get_restore_point()
        self.assertNotEqual(first["summary"], second["summary"])
        self.assertEqual(second["summary"], db.snapshot_digest(pre_snap))
        # 单行存储：恢复点表始终至多 1 行
        with db._connect() as conn:
            n = conn.execute(
                "SELECT COUNT(*) AS c FROM migration_restore_point"
            ).fetchone()["c"]
        self.assertEqual(n, 1)

    def test_restore_brings_back_pre_import_data(self):
        db.upsert_entry("临时原料", ["芥末"])
        pre_import = db.export_snapshot()
        self._do_import()
        # 导入后：临时原料不存在
        self.assertNotIn("临时原料",
                         {e["ingredient"] for e in db.list_dict()})
        status = restore_point_status()
        point = status["point"]
        # 错误摘要不恢复、不消费令牌
        with self.assertRaises(RestoreConfirmError):
            confirm_restore("错误摘要", status["token"])
        self.assertEqual(db.export_snapshot()["dict"],
                         build_export()["data"]["dict"])  # 仍是导入后状态
        # 正确摘要 + 一次性令牌恢复
        result = confirm_restore(point["summary"], status["token"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["restored"], point["counts"])
        self.assertEqual(result["old_summary"], point["summary"])
        self.assertEqual(db.export_snapshot(), pre_import)
        self.assertIn("临时原料",
                      {e["ingredient"] for e in db.list_dict()})

    def test_restore_is_undoable_swap_chain(self):
        """导入 -> 恢复 -> 再恢复：每一步都交换恢复点，可反复撤回。"""
        state0 = db.export_snapshot()
        self._do_import()                       # 恢复点=state0，当前=导入内容
        s1 = restore_point_status()
        confirm_restore(s1["point"]["summary"], s1["token"])
        self.assertEqual(db.export_snapshot(), state0)  # 回到 state0
        # 恢复前的导入内容已交换为新恢复点（reason=restore）
        s2 = restore_point_status()
        self.assertEqual(s2["point"]["reason"], db.RESTORE_REASON_RESTORE)
        imported_snap = json.loads(self.original_text)["data"]
        self.assertEqual(s2["point"]["summary"],
                         db.snapshot_digest(imported_snap))
        # 再恢复 -> 又回到导入后内容（误恢复被撤回）
        confirm_restore(s2["point"]["summary"], s2["token"])
        self.assertEqual(db.export_snapshot(), imported_snap)
        # 再再恢复 -> 回到 state0
        s3 = restore_point_status()
        confirm_restore(s3["point"]["summary"], s3["token"])
        self.assertEqual(db.export_snapshot(), state0)

    def test_restore_token_one_time_bound_and_wrong_summary_keeps_it(self):
        self._do_import()
        status = restore_point_status()
        token, summary = status["token"], status["point"]["summary"]
        # 摘要不一致：拒绝且令牌未消费，可凭同一令牌改正后重试
        with self.assertRaises(RestoreConfirmError):
            confirm_restore(summary + "00", token)
        self.assertEqual(confirm_restore(summary, token)["ok"], True)
        # 一次性：令牌复用被拒
        with self.assertRaises(RestoreConfirmError):
            confirm_restore(summary, token)
        # 导入令牌不能用于恢复（用途绑定）
        st = restore_point_status()
        imp_out = precheck_package(self.original_text)
        with self.assertRaises(RestoreConfirmError):
            confirm_restore(st["point"]["summary"], imp_out["token"])
        # 恢复令牌不能用于导入
        with self.assertRaises(MigrationConfirmError):
            confirm_import(self.original_text, st["token"])
        # 无恢复点时恢复拒绝
        with db._connect() as conn:
            conn.execute("DELETE FROM migration_restore_point")
        with self.assertRaises(RestoreConfirmError):
            confirm_restore("x", "y")

    def test_restore_without_point(self):
        with self.assertRaises(db.RestoreError):
            db.restore_from_point()

    def test_restore_empty_snapshot_roundtrip(self):
        """导入空迁移包：空数据也存恢复点，恢复后回到导入前。"""
        empty = {"format": EXPORT_FORMAT, "version": EXPORT_VERSION,
                 "data": {"dict": [], "aliases": [], "reviews": []}}
        empty["counts"] = {"dict": 0, "aliases": 0, "reviews": 0}
        empty["sha256"] = package_digest(empty)
        before = db.export_snapshot()
        result = self._do_import(pkg_text(empty))
        self.assertEqual(result["imported"], empty["counts"])
        self.assertEqual(db.table_counts(), empty["counts"])
        point = db.get_restore_point()
        self.assertEqual(point["counts"],
                         {"dict": 4, "aliases": 1, "reviews": 2})
        status = restore_point_status()
        confirm_restore(status["point"]["summary"], status["token"])
        self.assertEqual(db.export_snapshot(), before)

    def test_summary_case_insensitive_input(self):
        self._do_import()
        status = restore_point_status()
        upper = "  " + status["point"]["summary"].upper() + "  "
        # 大小写 / 首尾空白容错
        self.assertEqual(
            confirm_restore(upper, status["token"])["ok"], True)

    def test_concurrent_import_confirm_only_one_takes_effect(self):
        """并发确认同一文件：一次性令牌保证只有一次生效。"""
        out = precheck_package(self.original_text)
        token = out["token"]
        results = []

        def worker():
            try:
                results.append(confirm_import(self.original_text, token))
            except MigrationConfirmError as exc:
                results.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        oks = [r for r in results if not isinstance(r, Exception)]
        errs = [r for r in results if isinstance(r, Exception)]
        self.assertEqual(len(oks), 1)
        self.assertEqual(len(errs), 3)
        # 导入恰好生效一次：数据 = 迁移包内容，恢复点恰好保存一份
        self.assertEqual(db.export_snapshot(),
                         json.loads(self.original_text)["data"])
        with db._connect() as conn:
            n = conn.execute(
                "SELECT COUNT(*) AS c FROM migration_restore_point"
            ).fetchone()["c"]
        self.assertEqual(n, 1)

    def test_concurrent_restore_only_one_token_succeeds(self):
        """并发恢复共用同一令牌：只有一次生效，其余拒绝且不破坏数据。"""
        self._do_import()
        status = restore_point_status()
        token, summary = status["token"], status["point"]["summary"]
        imported_data = json.loads(self.original_text)["data"]
        outcomes = []

        def worker():
            try:
                outcomes.append(confirm_restore(summary, token))
            except RestoreConfirmError as exc:
                outcomes.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sum(1 for o in outcomes if not isinstance(o, Exception)),
                         1)
        # 只交换一次：当前是恢复点内容（state0），最新恢复点是导入内容
        self.assertEqual(db.snapshot_digest(db.export_snapshot()), summary)
        latest = db.get_restore_point()
        self.assertEqual(latest["summary"],
                         db.snapshot_digest(imported_data))
        self.assertEqual(latest["reason"], db.RESTORE_REASON_RESTORE)


if __name__ == "__main__":
    unittest.main()
