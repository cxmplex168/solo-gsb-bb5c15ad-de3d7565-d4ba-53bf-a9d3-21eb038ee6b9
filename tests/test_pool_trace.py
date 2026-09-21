"""池化检测阳性溯源测试（不依赖 FastAPI，走 db + review + pool_trace 全链路）。

覆盖：
- 仅允许选择一条时效正常记录，读取其当前配方（两版取新版）配料树的
  叶子原料（同名叶子去重、展示名取首次出现）；
- 「池名：原料列表：阳性/阴性」逐行解析：三段格式、状态精确取值、
  空池名、重名、引用非配方原料整批拒绝（逐行给出问题）、同池重复
  原料折叠、注释空行、英文冒号；
- 子集动态规划：最少嫌疑原料命中集、并列按原料名升序序列字典序取
  唯一、并列解总数，对照暴力枚举；阴性池排除全部原料；
- 阳性池被阴性证据完全排除（含空阳性池）时无解并给出冲突池及排除链；
- 缺失 / 非法 ID / 需复核记录拒绝并说明；全程只读、不写库。
"""
import os
import tempfile
import unittest
from itertools import combinations

_DB_DIR = tempfile.mkdtemp(prefix="pool-trace-")
os.environ["DB_PATH"] = os.path.join(_DB_DIR, "trace.db")

from app import db  # noqa: E402
from app.analysis import normalize_name  # noqa: E402
from app.pool_trace import (PoolTraceError, build_pool_trace_preview,  # noqa: E402
                            leaf_ingredients_of, list_trace_records,
                            parse_pools, solve_trace)
from app.recheck import build_recheck  # noqa: E402
from app.review import analyze  # noqa: E402

DICT_TEXT = """\
小麦粉：麸质谷物
乳清粉：乳
乳糖：乳
奶粉：乳
可可脂：
大豆卵磷脂：大豆
鸡蛋：蛋类
椰子粉：坚果
芝麻：芝麻
食盐：
"""
TEXT_A = "配料表：小麦粉，白砂糖，巧克力酱（可可脂，奶粉（乳清粉，乳糖），大豆卵磷脂），鸡蛋，食盐"
TEXT_B = "配料表：小麦粉，椰子粉，鸡蛋，芝麻酱（芝麻），食盐"


def make_review(text_a, text_b=None, name="溯源测试", with_fingerprint=True):
    result, fp = analyze(text_a=text_a, text_b=text_b, label_a="现版配方",
                         label_b="新版配方", declared=[])
    return db.create_review(
        name=name, declared=[], text_a=text_a, text_b=text_b,
        label_a="现版配方", label_b="新版配方", result=result,
        dict_fingerprint=fp if with_fingerprint else None)


def brute_force(pools):
    """暴力枚举候选原料子集，返回 (最少嫌疑数, 原料名键, 并列解数)。

    pools 为 solve_trace 同口径的已解析池；候选 = 出现在阳性池且不被
    阴性池排除的原料；阳性池全空 / 全排除时返回 None（无解）。
    """
    negatives = [p for p in pools if p["status"] == "negative"]
    positives = [p for p in pools if p["status"] == "positive"]
    excluded = set()
    for p in negatives:
        excluded |= set(p["ingredients"])
    candidates = sorted(
        {x for p in positives for x in p["ingredients"]} - excluded)
    target_sets = [set(p["ingredients"]) - excluded for p in positives]
    if any(not s for s in target_sets):
        return None
    best, best_key, ways = None, None, 0
    for r in range(len(candidates) + 1):
        for combo in combinations(candidates, r):
            chosen = set(combo)
            if all(chosen & s for s in target_sets):
                key = tuple(sorted(combo))
                if best is None or r < best:
                    best, best_key, ways = r, key, 1
                elif r == best:
                    ways += 1
                    if key < best_key:
                        best_key = key
    return best, best_key, ways


class LeafIngredientsTest(unittest.TestCase):
    """叶子原料提取：只取叶子、同名去重、两版取新版。"""

    def setUp(self):
        db.DB_PATH = os.path.join(
            _DB_DIR, f"leaf_{self.id().rsplit('.', 1)[-1]}.db")
        db.init_db()
        db.bulk_import(DICT_TEXT)

    def test_leaves_only_and_dedup(self):
        rid = make_review(
            "配料表：巧克力酱（奶粉（乳清粉）），夹心酱（奶粉（乳清粉））")
        review = db.get_review(rid)
        leaves = leaf_ingredients_of(review)
        names = [x["name"] for x in leaves]
        # 复合配料巧克力酱 / 夹心酱 / 奶粉不是叶子；乳清粉两处出现折叠为一种
        self.assertEqual(names, ["乳清粉"])
        self.assertEqual(leaves[0]["occurrences"], 2)
        self.assertEqual(sorted(leaves[0]["paths"]),
                         [["夹心酱", "奶粉", "乳清粉"],
                          ["巧克力酱", "奶粉", "乳清粉"]])

    def test_leaves_from_current_version_b(self):
        rid = make_review(TEXT_A, TEXT_B, name="两版")
        review = db.get_review(rid)
        leaves = [x["name"] for x in leaf_ingredients_of(review)]
        # 当前配方取新版：椰子粉、芝麻（在芝麻酱下）、鸡蛋、小麦粉、食盐；
        # 列表按展示名 Unicode 升序
        self.assertEqual(leaves, ["小麦粉", "椰子粉", "芝麻", "食盐", "鸡蛋"])
        rec = {r["id"]: r for r in list_trace_records()}[rid]
        self.assertEqual(rec["source_version"], "新版配方")
        self.assertTrue(rec["eligible"])
        self.assertEqual(rec["leaf_ingredients"],
                         [{"name": n,
                           "occurrences": 1,
                           "paths": [[n]] if n != "芝麻"
                           else [["芝麻酱", "芝麻"]]}
                          for n in ["小麦粉", "椰子粉", "芝麻", "食盐", "鸡蛋"]])


class ParseTest(unittest.TestCase):
    """检测池行解析：格式、状态、重名、非配方原料、去重、注释。"""

    LEAVES = [{"name": "乳清粉", "occurrences": 1, "paths": [["乳清粉"]]},
              {"name": "鸡蛋", "occurrences": 1, "paths": [["鸡蛋"]]},
              {"name": "可可脂", "occurrences": 1, "paths": [["可可脂"]]}]

    def test_basic_parse(self):
        pools = parse_pools(
            "# 注释\n\n一号池: 乳清粉, 鸡蛋 :阳性\n二号池：可可脂：阴性",
            self.LEAVES)
        self.assertEqual([p["name"] for p in pools], ["一号池", "二号池"])
        self.assertEqual(pools[0]["status"], "positive")
        self.assertEqual(pools[0]["ingredients"], ["乳清粉", "鸡蛋"])
        self.assertEqual(pools[1]["status"], "negative")
        self.assertEqual(pools[1]["ingredients"], ["可可脂"])

    def test_same_pool_duplicate_folded(self):
        pools = parse_pools("P：乳清粉，乳清粉，鸡蛋：阳性", self.LEAVES)
        self.assertEqual(pools[0]["ingredients"], ["乳清粉", "鸡蛋"])

    def test_format_status_duplicate_unknown(self):
        with self.assertRaises(PoolTraceError) as ctx:
            parse_pools(
                "只有池名\n  ：鸡蛋：阳性\nQ：鸡蛋：待定\n"
                "X：外星料：阳性\nP：鸡蛋：阳性\nP：乳清粉：阴性",
                self.LEAVES)
        types = [(p["line"], p["type"]) for p in ctx.exception.problems]
        self.assertEqual(types, [(1, "format"), (2, "empty_name"),
                                 (3, "bad_status"), (4, "unknown_ingredient"),
                                 (6, "duplicate_name")])

    def test_status_word_exact(self):
        # 「+」「阳」「正」等简写都视为非法状态
        with self.assertRaises(PoolTraceError) as ctx:
            parse_pools("P：鸡蛋：+", self.LEAVES)
        self.assertEqual(ctx.exception.problems[0]["type"], "bad_status")
        with self.assertRaises(PoolTraceError) as ctx:
            parse_pools("P：鸡蛋：阳", self.LEAVES)
        self.assertEqual(ctx.exception.problems[0]["type"], "bad_status")
        with self.assertRaises(PoolTraceError) as ctx:
            parse_pools("P：鸡蛋：阳性：阴性", self.LEAVES)
        self.assertEqual(ctx.exception.problems[0]["type"], "format")


class SolveTest(unittest.TestCase):
    """子集动态规划纯计算：最少命中、并列字典序（对照暴力枚举）、阴性排除。"""

    def _pools(self, spec):
        """spec: [(name, [原料], "positive"/"negative"), ...]。

        与 parse_pools 同口径：keys 为归一化键，ingredients 为展示名。
        """
        return [{"line": i + 1, "name": n, "ingredients": sorted(al),
                 "keys": {normalize_name(x) for x in al}, "status": s}
                for i, (n, al, s) in enumerate(spec)]

    def _leaves(self, pools):
        names = sorted({x for p in pools for x in p["ingredients"]})
        return [{"name": n, "occurrences": 1, "paths": [[n]]} for n in names]

    def test_single_suspect_covers_all(self):
        pools = self._pools([
            ("P1", ["乳清粉", "鸡蛋"], "positive"),
            ("P2", ["可可脂", "鸡蛋"], "positive"),
            ("N1", ["可可脂"], "negative"),
        ])
        r = solve_trace(pools, self._leaves(pools))
        self.assertTrue(r["feasible"])
        self.assertEqual(r["min_suspect_count"], 1)
        self.assertEqual(r["optimal_count"], 1)
        self.assertEqual([s["ingredient"] for s in r["suspects"]], ["鸡蛋"])
        self.assertEqual(r["suspects"][0]["explains"], ["P1", "P2"])
        # 阴性池排除可可脂，候选只剩 乳清粉 / 鸡蛋
        self.assertEqual(r["candidate_ingredients"], ["乳清粉", "鸡蛋"])
        self.assertEqual(r["excluded_ingredients"],
                         [{"ingredient": "可可脂", "excluded_by": ["N1"]}])
        # 阳性池回显命中嫌疑与未被排除原料
        overview = {p["name"]: p for p in r["pools"]}
        self.assertEqual(overview["P2"]["suspects"], ["鸡蛋"])
        self.assertEqual(overview["P2"]["survivors"], ["鸡蛋"])
        self.assertEqual(overview["N1"]["excludes"], ["可可脂"])

    def test_tie_break_and_count(self):
        # 丙同时出现在两个阳性池 → 唯一单原料解
        pools = self._pools([
            ("P1", ["甲", "丙"], "positive"),
            ("P2", ["乙", "丙"], "positive"),
        ])
        r = solve_trace(pools, self._leaves(pools))
        self.assertTrue(r["feasible"])
        self.assertEqual(r["min_suspect_count"], 1)
        self.assertEqual(r["optimal_count"], 1)
        self.assertEqual([s["ingredient"] for s in r["suspects"]], ["丙"])

        # 三角并列：P1={甲,乙}, P2={乙,丙}, P3={丙,甲}，单原料均无法覆盖
        # 全部，最少 2 个原料共 3 个并列解（甲乙 / 甲丙 / 乙丙），按原料
        # 名 Unicode 序列字典序展示最小者（丙 U+4E19 < 乙 U+4E59）
        pools = self._pools([
            ("P1", ["甲", "乙"], "positive"),
            ("P2", ["乙", "丙"], "positive"),
            ("P3", ["丙", "甲"], "positive"),
        ])
        r = solve_trace(pools, self._leaves(pools))
        self.assertEqual(r["min_suspect_count"], 2)
        self.assertEqual(r["optimal_count"], 3)
        self.assertEqual([s["ingredient"] for s in r["suspects"]], ["丙", "乙"])
        by_name = {s["ingredient"]: s for s in r["suspects"]}
        self.assertEqual(by_name["乙"]["explains"], ["P1", "P2"])
        self.assertEqual(by_name["丙"]["explains"], ["P2", "P3"])

    def test_matches_brute_force(self):
        import random
        random.seed(9)
        universe = ["A", "B", "C", "D", "E"]
        for trial in range(120):
            spec = []
            pool_names = []
            for i in range(random.randint(1, 5)):
                name = f"P{i}"
                pool_names.append(name)
                spec.append((name, random.sample(universe,
                             random.randint(1, 3)), "positive"))
            for i in range(random.randint(0, 2)):
                spec.append((f"N{i}", random.sample(universe,
                             random.randint(1, 2)), "negative"))
            pools = self._pools(spec)
            r = solve_trace(pools, self._leaves(pools))
            bf = brute_force(pools)
            if bf is None or bf[0] is None:
                self.assertFalse(r["feasible"])
                self.assertTrue(r["conflicts"])
            else:
                self.assertTrue(r["feasible"])
                self.assertEqual(r["min_suspect_count"], bf[0])
                self.assertEqual(r["optimal_count"], bf[2])
                self.assertEqual(tuple(s["ingredient"]
                                       for s in r["suspects"]), bf[1])

    def test_negative_excludes_candidate(self):
        # 阴性池排除后唯一可能原料仍可解释：N 排除 鸡蛋/可可脂，
        # P1 剩乳清粉、P2 只剩乳清粉
        pools = self._pools([
            ("P1", ["乳清粉", "鸡蛋"], "positive"),
            ("P2", ["乳清粉", "可可脂"], "positive"),
            ("N", ["鸡蛋", "可可脂"], "negative"),
        ])
        r = solve_trace(pools, self._leaves(pools))
        self.assertTrue(r["feasible"])
        self.assertEqual([s["ingredient"] for s in r["suspects"]], ["乳清粉"])
        self.assertEqual(r["optimal_count"], 1)

    def test_conflict_positive_fully_excluded(self):
        pools = self._pools([
            ("P1", ["鸡蛋"], "positive"),
            ("N1", ["鸡蛋"], "negative"),
        ])
        r = solve_trace(pools, self._leaves(pools))
        self.assertFalse(r["feasible"])
        self.assertEqual(r["min_suspect_count"], None)
        [c] = r["conflicts"]
        self.assertEqual(c["name"], "P1")
        self.assertEqual(c["reason"], "excluded")
        self.assertEqual(c["excluded_ingredients"], ["鸡蛋"])
        self.assertEqual(c["excluded_by"], {"鸡蛋": ["N1"]})

    def test_conflict_empty_positive(self):
        pools = self._pools([
            ("空池", [], "positive"),
            ("P2", ["鸡蛋"], "positive"),
        ])
        r = solve_trace(pools, self._leaves(pools))
        self.assertFalse(r["feasible"])
        self.assertEqual([c["name"] for c in r["conflicts"]], ["空池"])
        self.assertEqual(r["conflicts"][0]["reason"], "empty")

    def test_no_positive_pools_empty_solution(self):
        pools = self._pools([("N1", ["鸡蛋"], "negative"),
                            ("N2", ["可可脂"], "negative")])
        r = solve_trace(pools, self._leaves(pools))
        self.assertTrue(r["feasible"])
        self.assertEqual(r["min_suspect_count"], 0)
        self.assertEqual(r["optimal_count"], 1)
        self.assertEqual(r["suspects"], [])
        self.assertEqual(r["candidate_ingredients"], [])

    def test_ingredient_not_in_any_positive_not_candidate(self):
        # 仅出现在阴性池里的原料既不参与求解，也不列为候选
        pools = self._pools([("P1", ["鸡蛋"], "positive"),
                            ("N1", ["可可脂", "乳清粉"], "negative")])
        r = solve_trace(pools, self._leaves(pools))
        self.assertTrue(r["feasible"])
        self.assertEqual([s["ingredient"] for s in r["suspects"]], ["鸡蛋"])
        self.assertEqual(
            {e["ingredient"] for e in r["excluded_ingredients"]},
            {"可可脂", "乳清粉"})

    def test_explains_all_positive_pools_listed(self):
        pools = self._pools([
            ("P1", ["甲", "乙"], "positive"),
            ("P2", ["甲"], "positive"),
            ("P3", ["乙"], "positive"),
        ])
        r = solve_trace(pools, self._leaves(pools))
        # 单原料无法覆盖全部（甲缺 P3、乙缺 P2），故最少 2 个原料，且
        # (甲,乙) 是唯一 2 原料解
        self.assertEqual(r["min_suspect_count"], 2)
        self.assertEqual(r["optimal_count"], 1)
        by_name = {s["ingredient"]: s for s in r["suspects"]}
        self.assertEqual(by_name["甲"]["explains"], ["P1", "P2"])
        self.assertEqual(by_name["乙"]["explains"], ["P1", "P3"])


class PreviewFlowTest(unittest.TestCase):
    """全链路：记录资格、两版取 B、冲突、需复核 / 缺失拒绝、只读。"""

    def setUp(self):
        db.DB_PATH = os.path.join(
            _DB_DIR, f"traceflow_{self.id().rsplit('.', 1)[-1]}.db")
        db.init_db()
        db.bulk_import(DICT_TEXT)

    def test_leaves_from_current_version(self):
        rid = make_review(TEXT_A, TEXT_B, name="两版")
        r = build_pool_trace_preview(rid, "P：椰子粉，芝麻：阳性")
        leaves = [x["name"] for x in r["review"]["leaf_ingredients"]]
        self.assertEqual(leaves, ["小麦粉", "椰子粉", "芝麻", "食盐", "鸡蛋"])
        self.assertTrue(r["feasible"])
        self.assertEqual(r["min_suspect_count"], 1)

    def test_feasible_trace(self):
        rid = make_review(TEXT_A)
        text = ("池一：乳清粉，鸡蛋：阳性\n"
                "池二：乳糖，鸡蛋，可可脂：阳性\n"
                "对照池：可可脂，食盐：阴性")
        r = build_pool_trace_preview(rid, text)
        self.assertTrue(r["feasible"])
        # 鸡蛋同时出现在两个阳性池且未被排除 → 唯一单原料解
        self.assertEqual([s["ingredient"] for s in r["suspects"]], ["鸡蛋"])
        self.assertEqual(r["suspects"][0]["explains"], ["池一", "池二"])
        self.assertEqual(r["optimal_count"], 1)
        self.assertEqual(r["negative_pools"], ["对照池"])

    def test_conflict_returns_no_solution(self):
        rid = make_review(TEXT_A)
        r = build_pool_trace_preview(
            rid, "P：乳清粉：阳性\nN：乳清粉，乳糖：阴性")
        self.assertFalse(r["feasible"])
        [c] = r["conflicts"]
        self.assertEqual(c["name"], "P")
        self.assertEqual(c["reason"], "excluded")
        self.assertEqual(c["excluded_ingredients"], ["乳清粉"])
        self.assertEqual(c["excluded_by"], {"乳清粉": ["N"]})

    def test_empty_positive_pool_conflict(self):
        rid = make_review(TEXT_A)
        r = build_pool_trace_preview(rid, "空池：：阳性")
        self.assertFalse(r["feasible"])
        self.assertEqual(r["conflicts"][0]["reason"], "empty")

    def test_only_negative_pools(self):
        rid = make_review(TEXT_A)
        r = build_pool_trace_preview(rid, "N1：食盐：阴性\nN2：可可脂：阴性")
        self.assertTrue(r["feasible"])
        self.assertEqual(r["min_suspect_count"], 0)
        self.assertEqual(r["suspects"], [])
        self.assertEqual(r["optimal_count"], 1)

    def test_batch_rejected_with_line_numbers(self):
        rid = make_review(TEXT_A)
        with self.assertRaises(PoolTraceError) as ctx:
            build_pool_trace_preview(
                rid,
                "P1：外星料：阳性\n坏格式\nP2：鸡蛋：阳性\nP2：乳清粉：阴性")
        types = {(p["line"], p["type"]) for p in ctx.exception.problems}
        self.assertIn((1, "unknown_ingredient"), types)
        self.assertIn((2, "format"), types)
        self.assertIn((4, "duplicate_name"), types)

    def test_empty_text_rejected(self):
        rid = make_review(TEXT_A)
        with self.assertRaises(PoolTraceError):
            build_pool_trace_preview(rid, "# 只有注释\n")

    def test_missing_and_invalid_record_blocked(self):
        with self.assertRaises(PoolTraceError) as ctx:
            build_pool_trace_preview(999999, "A：鸡蛋：阳性")
        self.assertEqual(ctx.exception.problems[0]["reason"], "missing")
        with self.assertRaises(PoolTraceError) as ctx:
            build_pool_trace_preview("x", "A：鸡蛋：阳性")
        self.assertEqual(ctx.exception.problems[0]["reason"], "invalid")

    def test_stale_record_blocked_until_rechecked(self):
        rid = make_review(TEXT_A)
        db.upsert_entry("乳清粉", ["乳", "花生"])  # 映射变更 → 需复核
        self.assertTrue(db.get_review(rid)["needs_recheck"])
        with self.assertRaises(PoolTraceError) as ctx:
            build_pool_trace_preview(rid, "A：乳清粉：阳性")
        [p] = ctx.exception.problems
        self.assertEqual(p["reason"], "stale")
        self.assertIn("需复核", p["message"])
        records = {r["id"]: r for r in list_trace_records()}
        self.assertFalse(records[rid]["eligible"])
        outcome = build_recheck(db.get_review(rid))
        db.update_review_result(rid, outcome["new_result"],
                                outcome["fingerprint"])
        r = build_pool_trace_preview(rid, "A：乳清粉：阳性")
        self.assertTrue(r["feasible"])

    def test_legacy_record_blocked(self):
        rid = make_review(TEXT_A, with_fingerprint=False)
        with self.assertRaises(PoolTraceError) as ctx:
            build_pool_trace_preview(rid, "A：鸡蛋：阳性")
        self.assertEqual(ctx.exception.problems[0]["reason"], "legacy")

    def test_readonly(self):
        rid = make_review(TEXT_A)
        before = db.get_review(rid)
        count = len(db.list_reviews())
        build_pool_trace_preview(rid, "P：鸡蛋：阳性")
        build_pool_trace_preview(rid, "P：乳清粉：阳性\nN：乳清粉：阴性")
        self.assertEqual(db.get_review(rid), before)
        self.assertEqual(len(db.list_reviews()), count)


if __name__ == "__main__":
    unittest.main()
