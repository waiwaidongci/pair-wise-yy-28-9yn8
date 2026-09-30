from __future__ import annotations
import os, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from database import ContinuityDB, DomainError


class OfflineMergeTest(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db"); os.close(fd)
        self.db = ContinuityDB(self.path)
        self.producer = self.db.add_user("制片", "producer")
        self.continuity = self.db.add_user("场记", "continuity")
        self.reviewer = self.db.add_user("审片", "reviewer")
        self.production = self.db.create_production("测试影片", "非线性拍摄", self.producer)
        self.scene = self.db.add_scene(self.production, "S01", "雨夜", 1)
        self.s1 = self.db.add_shot(self.scene, "S01-01", 2, 1, "受伤后", self.continuity)
        self.s2 = self.db.add_shot(self.scene, "S01-02", 1, 2, "受伤前", self.continuity)
        self.injury = self.db.add_element(self.production, "手臂伤痕", "injury", "monotonic", "只能加重")
        self.db.set_element_state(self.s1, self.injury, "重度", 3, "", self.continuity)
        self.db.set_element_state(self.s2, self.injury, "轻度", 1, "", self.continuity)
        self.conflict = self.db.check_scene(self.scene)[0]
        self.device = self.db.register_device("DEV-001", "山里拍摄设备")

    def tearDown(self):
        self.db.close(); os.unlink(self.path)

    def _state_payload(self, shot_id, element_id, value, numeric, note=""):
        return {"shot_id": shot_id, "element_id": element_id, "state_value": value,
                "numeric_value": numeric, "note": note, "user_id": self.continuity}

    def test_device_registration_and_duplicate(self):
        self.assertEqual(self.device, 1)
        with self.assertRaisesRegex(DomainError, "设备号已注册"):
            self.db.register_device("DEV-001", "重复")

    def test_shot_state_merge(self):
        # 场记在山里把 s2 的伤痕改成「中度」，基准版本为当前版本
        base = self.db.conn.execute("SELECT version FROM shots WHERE id=?", (self.s2,)).fetchone()["version"]
        record = self.db.upload_offline_record(
            self.device, 1, "shot_state",
            self._state_payload(self.s2, self.injury, "中度", 2, "山里补拍"),
            base,
        )
        self.assertEqual("merged", record["status"])
        # 状态已写入
        state = self.db.conn.execute(
            "SELECT state_value,numeric_value FROM element_states WHERE shot_id=? AND element_id=?",
            (self.s2, self.injury),
        ).fetchone()
        self.assertEqual("中度", state["state_value"])
        self.assertEqual(2, state["numeric_value"])
        # 镜头版本已递增
        ver = self.db.conn.execute("SELECT version FROM shots WHERE id=?", (self.s2,)).fetchone()["version"]
        self.assertEqual(base + 1, ver)

    def test_exemption_merge(self):
        base = self.db.conn.execute("SELECT version FROM shots WHERE id=?", (self.s2,)).fetchone()["version"]
        record = self.db.upload_offline_record(
            self.device, 1, "exemption",
            {"conflict_id": self.conflict["id"], "reason": "闪回镜头刻意叙事误差", "user_id": self.continuity},
            base,
        )
        self.assertEqual("merged", record["status"])
        exempt = self.db.conn.execute(
            "SELECT reason FROM exemptions WHERE conflict_id=?", (self.conflict["id"],)
        ).fetchone()
        self.assertEqual("闪回镜头刻意叙事误差", exempt["reason"])
        conflict = self.db.conn.execute(
            "SELECT status FROM conflicts WHERE id=?", (self.conflict["id"],)
        ).fetchone()
        self.assertEqual("exempted", conflict["status"])

    def test_idempotent_upload_returns_original_result(self):
        base = self.db.conn.execute("SELECT version FROM shots WHERE id=?", (self.s2,)).fetchone()["version"]
        first = self.db.upload_offline_record(
            self.device, 1, "shot_state",
            self._state_payload(self.s2, self.injury, "中度", 2), base)
        # 重复回传同一连续编号，沿用原结果，不重复写入
        second = self.db.upload_offline_record(
            self.device, 1, "shot_state",
            self._state_payload(self.s2, self.injury, "重度", 3), base)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["status"], second["status"])
        state = self.db.conn.execute(
            "SELECT state_value FROM element_states WHERE shot_id=? AND element_id=?",
            (self.s2, self.injury),
        ).fetchone()
        self.assertEqual("中度", state["state_value"])

    def test_studio_modified_shot_goes_to_pending_verification(self):
        base = self.db.conn.execute("SELECT version FROM shots WHERE id=?", (self.s2,)).fetchone()["version"]
        # 棚内改动了 s2
        self.db.set_element_state(self.s2, self.injury, "重度", 3, "棚内重做", self.continuity)
        record = self.db.upload_offline_record(
            self.device, 1, "shot_state",
            self._state_payload(self.s2, self.injury, "中度", 2), base)
        self.assertEqual("pending_verification", record["status"])
        self.assertIn("棚内改动", record["reason"])
        # 棚内改动未被覆盖
        state = self.db.conn.execute(
            "SELECT state_value,note FROM element_states WHERE shot_id=? AND element_id=?",
            (self.s2, self.injury),
        ).fetchone()
        self.assertEqual("重度", state["state_value"])
        self.assertEqual("棚内重做", state["note"])

    def test_two_devices_same_shot(self):
        dev2 = self.db.register_device("DEV-002", "第二台设备")
        base = self.db.conn.execute("SELECT version FROM shots WHERE id=?", (self.s2,)).fetchone()["version"]
        r1 = self.db.upload_offline_record(
            self.device, 1, "shot_state",
            self._state_payload(self.s2, self.injury, "中度", 2), base)
        self.assertEqual("merged", r1["status"])
        # 第二台设备基准版本落后，回传后待核对
        r2 = self.db.upload_offline_record(
            dev2, 1, "shot_state",
            self._state_payload(self.s2, self.injury, "重度", 3), base)
        self.assertEqual("pending_verification", r2["status"])

    def test_resume_from_merging_state(self):
        base = self.db.conn.execute("SELECT version FROM shots WHERE id=?", (self.s2,)).fetchone()["version"]
        # 模拟写入中断：一条记录停在 merging，状态尚未写入
        payload = self._state_payload(self.s2, self.injury, "中度", 2)
        payload_json = __import__("json").dumps(payload, ensure_ascii=False)
        self.db.conn.execute(
            "INSERT INTO offline_records(device_id,device_number,continuity_number,record_type,payload,base_shot_version,status,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,'merging',?,?)",
            (self.device, "DEV-001", 1, "shot_state", payload_json, base, "2026-01-01", "2026-01-01"),
        )
        self.db.conn.commit()
        summary = self.db.offline_summary()
        self.assertEqual(1, summary["resume_pending"])
        resumed = self.db.resume_pending()
        self.assertEqual(1, len(resumed["merged"]))
        done = self.db.conn.execute(
            "SELECT status FROM offline_records WHERE id=?", (1,)
        ).fetchone()
        self.assertEqual("merged", done["status"])

    def test_merge_device_records_in_continuity_order(self):
        base = self.db.conn.execute("SELECT version FROM shots WHERE id=?", (self.s2,)).fetchone()["version"]
        # 手动插入两条 pending 记录，连续编号 1 和 2，基准版本递增（模拟设备本地跟踪）
        for cont, value, numeric, note in [(1, "中度", 2, "第一次"), (2, "重度", 3, "第二次")]:
            payload = self._state_payload(self.s2, self.injury, value, numeric, note)
            payload_json = __import__("json").dumps(payload, ensure_ascii=False)
            self.db.conn.execute(
                "INSERT INTO offline_records(device_id,device_number,continuity_number,record_type,payload,base_shot_version,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,'pending',?,?)",
                (self.device, "DEV-001", cont, "shot_state", payload_json, base + cont - 1, "2026-01-01", "2026-01-01"),
            )
        self.db.conn.commit()
        result = self.db.merge_device_records(self.device)
        self.assertEqual(2, len(result["merged"]))
        # 按连续编号顺序合并：先 1（中度），后 2（重度），最终为重度
        state = self.db.conn.execute(
            "SELECT state_value FROM element_states WHERE shot_id=? AND element_id=?",
            (self.s2, self.injury),
        ).fetchone()
        self.assertEqual("重度", state["state_value"])
        rows = self.db.conn.execute(
            "SELECT continuity_number,status FROM offline_records WHERE device_id=? ORDER BY continuity_number",
            (self.device,),
        ).fetchall()
        self.assertEqual(1, rows[0]["continuity_number"])
        self.assertEqual(2, rows[1]["continuity_number"])
        self.assertEqual("merged", rows[0]["status"])
        self.assertEqual("merged", rows[1]["status"])

    def test_offline_summary_shows_source_device(self):
        base = self.db.conn.execute("SELECT version FROM shots WHERE id=?", (self.s2,)).fetchone()["version"]
        self.db.upload_offline_record(
            self.device, 1, "shot_state",
            self._state_payload(self.s2, self.injury, "中度", 2), base)
        summary = self.db.offline_summary()
        self.assertEqual(1, summary["total"])
        self.assertEqual("DEV-001", summary["records"][0]["device_number"])
        self.assertEqual("merged", summary["records"][0]["status"])
        self.assertIn("merged", summary["by_status"])


if __name__ == "__main__":
    unittest.main()
