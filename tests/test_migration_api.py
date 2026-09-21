"""迁移恢复保护的 HTTP 接口测试（FastAPI TestClient + 临时 SQLite 库）。

覆盖：
- ``GET /api/migration/restore-point``：初始无恢复点；成功导入后返回
  时间 / 来源 / 三类数量 / 摘要 + 一次性恢复令牌；预检响应同步携带；
- ``POST /api/migration/restore``：错摘要 / 坏令牌 / 复用令牌均 400
  且数据不变；摘要正确则一键恢复，返回新恢复点并恢复数据；误恢复可
  再调一次接口撤回。
"""
import json
import os
import tempfile
import unittest

_DB_DIR = tempfile.mkdtemp(prefix="allergen-restore-api-")
os.environ["DB_PATH"] = os.path.join(_DB_DIR, "restore_api.db")

from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.aliases import validate_alias_batch  # noqa: E402
from app.main import app  # noqa: E402
from app.review import analyze  # noqa: E402

DICT_TEXT = "小麦粉：麸质谷物\n乳清粉：乳\n"
TEXT_A = "配料表：小麦粉，乳清粉，食盐"


def make_review(name="接口测试"):
    result, fp = analyze(text_a=TEXT_A, text_b=None, label_a="现版配方",
                         label_b="新版配方", declared=["麸质谷物"])
    return db.create_review(
        name=name, declared=["麸质谷物"], text_a=TEXT_A, text_b=None,
        label_a="现版配方", label_b="新版配方", result=result,
        dict_fingerprint=fp)


class RestoreApiTest(unittest.TestCase):
    def setUp(self):
        # 每个用例使用独立库文件，避免恢复点相互干扰
        db.DB_PATH = os.path.join(
            _DB_DIR, self.id().replace(".", "_") + ".db")
        db.init_db()
        db.bulk_import(DICT_TEXT)
        db.bulk_upsert_aliases(
            validate_alias_batch("脱盐乳清：乳清粉", db.alias_existing())
            ["entries"])
        make_review()
        self.client = TestClient(app)

    def _export_text(self):
        r = self.client.get("/api/migration/export")
        self.assertEqual(r.status_code, 200)
        return r.text

    def _precheck(self, text):
        return self.client.post("/api/migration/precheck", json={"text": text})

    def _import(self, text):
        r = self._precheck(text)
        self.assertEqual(r.status_code, 200, r.text)
        token = r.json()["token"]
        return self.client.post("/api/migration/import",
                                json={"token": token, "text": text})

    def test_restore_point_empty_then_created_by_import(self):
        r = self.client.get("/api/migration/restore-point")
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json()["point"])
        self.assertIsNone(r.json()["token"])

        # 改出与迁移包不同的当前数据后导入
        text = self._export_text()
        db.upsert_entry("临时原料", ["芥末"])
        before = db.export_snapshot()
        r = self._import(text)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["ok"])
        point = body["restore_point"]
        self.assertEqual(point["reason"], "migration_import")
        self.assertEqual(point["counts"],
                         {"dict": 3, "aliases": 1, "reviews": 1})
        self.assertEqual(point["summary"], db.snapshot_digest(before))

        r = self.client.get("/api/migration/restore-point")
        status = r.json()
        self.assertEqual(status["point"], point)
        self.assertTrue(status["token"])
        # 预检响应同步携带恢复点
        pre = self._precheck(text).json()
        self.assertEqual(pre["restore_point"]["summary"], point["summary"])

    def test_restore_rejects_bad_summary_bad_and_reused_token(self):
        text = self._export_text()
        db.upsert_entry("临时原料", ["芥末"])
        self.assertEqual(self._import(text).status_code, 200)
        status = self.client.get("/api/migration/restore-point").json()
        summary, token = status["point"]["summary"], status["token"]

        # 错摘要 400 且数据不变（令牌也不消费）
        post_import = db.export_snapshot()
        r = self.client.post("/api/migration/restore",
                             json={"token": token, "summary": "错"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("摘要", r.json()["detail"]["message"])
        self.assertEqual(db.export_snapshot(), post_import)

        # 坏令牌 400
        r = self.client.post("/api/migration/restore",
                             json={"token": "坏令牌", "summary": summary})
        self.assertEqual(r.status_code, 400)

        # 正确恢复（摘要大小写容错）
        r = self.client.post("/api/migration/restore",
                             json={"token": token, "summary": summary.upper()})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["restored"], status["point"]["counts"])
        self.assertIn("临时原料",
                      {e["ingredient"] for e in db.list_dict()})
        # 返回的是恢复前数据交换成的新恢复点（误恢复可撤回）
        self.assertEqual(body["restore_point"]["reason"], "restore")

        # 令牌一次性：复用 400
        r = self.client.post("/api/migration/restore",
                             json={"token": token, "summary": summary})
        self.assertEqual(r.status_code, 400)

    def test_restore_undo_chain_via_api(self):
        text = self._export_text()  # 迁移包不含「临时原料」
        db.upsert_entry("临时原料", ["芥末"])  # 当前数据与迁移包可区分
        self.assertEqual(self._import(text).status_code, 200)
        imported = json.loads(text)["data"]
        self.assertEqual(db.snapshot_digest(db.export_snapshot()),
                         db.snapshot_digest(imported))

        # 恢复 -> 回到导入前（含临时原料）；再恢复 -> 回到导入后（撤回误恢复）
        st = self.client.get("/api/migration/restore-point").json()
        pre_restore_digest = db.snapshot_digest(db.export_snapshot())
        r = self.client.post("/api/migration/restore",
                             json={"token": st["token"],
                                   "summary": st["point"]["summary"]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(st["point"]["summary"],
                         db.snapshot_digest(db.export_snapshot()))
        self.assertNotEqual(db.snapshot_digest(db.export_snapshot()),
                            pre_restore_digest)
        self.assertIn("临时原料",
                      {e["ingredient"] for e in db.list_dict()})

        st2 = self.client.get("/api/migration/restore-point").json()
        self.assertEqual(st2["point"]["reason"], "restore")
        r = self.client.post("/api/migration/restore",
                             json={"token": st2["token"],
                                   "summary": st2["point"]["summary"]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(db.snapshot_digest(db.export_snapshot()),
                         db.snapshot_digest(imported))
        self.assertNotIn("临时原料",
                         {e["ingredient"] for e in db.list_dict()})

    def test_restore_without_point_returns_400(self):
        r = self.client.post(
            "/api/migration/restore",
            json={"token": "任意", "summary": "0" * 64})
        self.assertEqual(r.status_code, 400)
        self.assertIn("恢复点", r.json()["detail"]["message"])


if __name__ == "__main__":
    unittest.main()
