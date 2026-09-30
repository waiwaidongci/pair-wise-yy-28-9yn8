import os, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from database import ContinuityDB, DomainError


class SyncTestBase(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db"); os.close(fd)
        self.db = ContinuityDB(self.path)
        self.producer = self.db.add_user("制片", "producer")
        self.continuity = self.db.add_user("场记", "continuity")
        self.reviewer = self.db.add_user("审片", "reviewer")
        self.production = self.db.create_production("测试影片", "离线回传", self.producer)
        self.scene = self.db.add_scene(self.production, "S01", "雨夜", 1)
        self.s1 = self.db.add_shot(self.scene, "S01-01", 2, 1, "受伤后", self.continuity)
        self.s2 = self.db.add_shot(self.scene, "S01-02", 1, 2, "受伤前", self.continuity)
        self.injury = self.db.add_element(self.production, "手臂伤痕", "injury", "monotonic", "只能加重")
        self.db.set_element_state(self.s1, self.injury, "重度", 3, "", self.continuity)
        self.db.set_element_state(self.s2, self.injury, "轻度", 1, "", self.continuity)
        conflicts = self.db.check_scene(self.scene)
        self.conflict = conflicts[0]
        self.assertEqual("regression", self.conflict["kind"])

    def tearDown(self):
        self.db.close(); os.unlink(self.path)

    def state_entry(self, seq, shot, base_version, value, numeric, user=None):
        return {"seq": seq, "kind": "state", "shot_id": shot, "base_version": base_version,
                "element_id": self.injury, "state_value": value, "numeric_value": numeric,
                "note": "山里离线记录", "user_id": user or self.continuity}

    def exemption_entry(self, seq, shot, base_version, conflict=None):
        return {"seq": seq, "kind": "exemption", "shot_id": shot, "base_version": base_version,
                "conflict_id": conflict or self.conflict["id"],
                "reason": "闪回镜头刻意保留伤痕误差", "reviewer_id": self.reviewer}


class OfflineSyncTest(SyncTestBase):
    def test_state_merge_bumps_version_and_rechecks_conflicts(self):
        # s2 当前 version=1（set_element_state 提升过一次），离线端基于该版本记录
        result = self.db.sync_batch("slate-A7", [self.state_entry(0, self.s2, 1, "重度", 3)])
        self.assertEqual(1, result["merged"]); self.assertEqual(0, result["review"])
        row = self.db.conn.execute("SELECT version,status FROM shots WHERE id=?", (self.s2,)).fetchone()
        self.assertEqual(2, row["version"])
        state = self.db.conn.execute("SELECT state_value,note FROM element_states WHERE shot_id=? AND element_id=?",
                                     (self.s2, self.injury)).fetchone()
        self.assertEqual("重度", state["state_value"]); self.assertEqual("山里离线记录", state["note"])
        # 伤势对齐后冲突应消失
        self.assertEqual([], self.db.list_conflicts(self.scene))

    def test_shed_change_marks_review_and_never_overwrites(self):
        # 棚内先改了 s2（版本从 1 变 2），离线端仍带着 base_version=1 回传
        self.db.set_element_state(self.s2, self.injury, "中度", 2, "棚内调整", self.producer)
        result = self.db.sync_batch("slate-A7", [self.state_entry(0, self.s2, 1, "重度", 3)])
        self.assertEqual(0, result["merged"]); self.assertEqual(1, result["review"])
        state = self.db.conn.execute("SELECT state_value,note FROM element_states WHERE shot_id=? AND element_id=?",
                                     (self.s2, self.injury)).fetchone()
        # 棚内改动原封不动
        self.assertEqual("中度", state["state_value"]); self.assertEqual("棚内调整", state["note"])
        review = self.db.list_sync_entries(status_filter="review")["entries"]
        self.assertEqual(1, len(review)); self.assertEqual("version_mismatch", review[0]["result"]["reason"])
        self.assertEqual("slate-A7", review[0]["device_id"])

    def test_two_devices_same_shot_merge_or_review_independently(self):
        # 设备 B 基于版本 1 先合并成功；设备 A 同样基于版本 1，合并时应发现版本已变
        self.db.sync_batch("slate-B2", [self.state_entry(0, self.s2, 1, "中度", 2)])
        result = self.db.sync_batch("slate-A7", [self.state_entry(0, self.s2, 1, "重度", 3)])
        self.assertEqual(1, result["review"])
        entries = self.db.list_sync_entries()["entries"]
        by_device = {e["device_id"]: e["status"] for e in entries}
        self.assertEqual({"slate-B2": "merged", "slate-A7": "review"}, by_device)
        progress = {d["device_id"]: d for d in self.db.sync_progress()["devices"]}
        self.assertEqual(1, progress["slate-B2"]["merged"]); self.assertEqual(1, progress["slate-A7"]["review"])

    def test_duplicate_upload_keeps_original_result(self):
        first = self.db.sync_batch("slate-A7", [self.state_entry(0, self.s2, 1, "重度", 3)])
        first_id = first["results"][0]["id"]
        # 重复回传，即便 payload 改成别的值也沿用原结果
        again = self.db.sync_batch("slate-A7", [self.state_entry(0, self.s2, 1, "轻度", 1)])
        self.assertEqual(1, again["merged"]); self.assertEqual(first_id, again["results"][0]["id"])
        self.assertEqual(1, self.db.conn.execute("SELECT COUNT(*) FROM sync_entries WHERE device_id='slate-A7'").fetchone()[0])

    def test_exemption_merge_then_shed_change_blocks_later_exemption(self):
        result = self.db.sync_batch("slate-A7", [self.exemption_entry(0, self.s2, 1)])
        self.assertEqual(1, result["merged"])
        conflict = self.db.conn.execute("SELECT status FROM conflicts WHERE id=?", (self.conflict["id"],)).fetchone()
        self.assertEqual("exempted", conflict["status"])
        self.assertEqual(1, self.db.conn.execute("SELECT COUNT(*) FROM exemptions").fetchone()[0])

    def test_exemption_review_when_shed_changed_target_shot(self):
        # 棚内先改了冲突的目标镜头 s2（版本 1 -> 2），离线豁免基于旧版本 -> 待核对，绝不写豁免
        self.db.set_element_state(self.s2, self.injury, "中度", 2, "棚内调整", self.producer)
        open_conflict = self.db.check_scene(self.scene)[0]
        # 离线端在棚内改动前记录，基线仍是版本 1
        result = self.db.sync_batch("slate-B2", [self.exemption_entry(0, self.s2, 1, open_conflict["id"])])
        self.assertEqual(1, result["review"]); self.assertEqual(0, result["merged"])
        self.assertEqual(0, self.db.conn.execute("SELECT COUNT(*) FROM exemptions").fetchone()[0])
        still_open = self.db.conn.execute("SELECT status FROM conflicts WHERE id=?", (open_conflict["id"],)).fetchone()
        self.assertEqual("open", still_open["status"])

    def test_resume_after_restart_picks_up_pending(self):
        # seq0 可以合并；seq1 模拟写入后、处理前服务中断：直接落一条 pending 记录（元素已不存在）
        import json
        from datetime import datetime
        result = self.db.sync_batch("slate-A7", [self.state_entry(0, self.s2, 1, "重度", 3)])
        self.assertEqual(1, result["merged"])
        self.db.conn.execute(
            "INSERT INTO sync_entries(device_id,seq,shot_id,base_version,kind,payload,created_at,status) "
            "VALUES(?,?,?,?,?,?,?,'pending')",
            ("slate-A7", 1, self.s2, 2, "state", json.dumps(
                {"element_id": 99999, "state_value": "x", "numeric_value": 0,
                 "note": "", "user_id": self.continuity}), datetime.now().isoformat()),
        )
        self.db.conn.commit()
        # 模拟服务重开：续传从未完成条目接着走，悬空元素安全落为待核对而非报错
        self.db.close(); self.db = ContinuityDB(self.path)
        resumed = self.db.resume_sync()["devices"][0]
        self.assertEqual("slate-A7", resumed["device_id"])
        self.assertEqual(0, resumed["pending"]); self.assertEqual(1, resumed["review"]); self.assertEqual(1, resumed["merged"])
        dangling = self.db.conn.execute("SELECT status,result FROM sync_entries WHERE device_id='slate-A7' AND seq=1").fetchone()
        self.assertEqual("review", dangling["status"])
        self.assertIn("element_missing", dangling["result"])

    def test_batch_validation_rejects_bad_seqs(self):
        good = self.state_entry(0, self.s2, 1, "重度", 3)
        with self.assertRaisesRegex(DomainError, "编号"):
            self.db.sync_batch("slate-A7", [good, {**good, "seq": 0}])
        with self.assertRaisesRegex(DomainError, "设备号"):
            self.db.sync_batch("  ", [good])
        # 校验失败不能写入任何条目
        self.assertEqual(0, self.db.conn.execute("SELECT COUNT(*) FROM sync_entries").fetchone()[0])

    def test_multi_entry_batch_with_mixed_outcomes_in_sequence(self):
        # seq0 合并使 s2 版本升到 2；seq1 仍基于版本 1 -> 待核对；seq2 基于版本 2 -> 继续合并
        entries = [
            self.state_entry(0, self.s2, 1, "中度", 2),
            self.state_entry(1, self.s2, 1, "重度", 3),
            self.state_entry(2, self.s2, 2, "重度", 3),
        ]
        result = self.db.sync_batch("slate-A7", entries)
        statuses = [e["status"] for e in result["results"]]
        self.assertEqual(["merged", "review", "merged"], statuses)
        self.assertEqual(2, result["merged"]); self.assertEqual(1, result["review"]); self.assertEqual(0, result["pending"])

    def test_exemption_entry_rejects_wrong_shot(self):
        bad = self.exemption_entry(0, self.s1, 1)  # 该冲突的 to_shot 是 s2
        with self.assertRaisesRegex(DomainError, "不匹配"):
            self.db.sync_batch("slate-A7", [bad])


if __name__ == "__main__":
    unittest.main()
