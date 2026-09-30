from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


class DomainError(ValueError):
    """Business rule violation."""


ELEMENT_KINDS = {"character", "costume", "prop", "injury"}
RULES = {"stable", "monotonic", "allowed"}


class ContinuityDB:
    """Non-linear film continuity checker with reviewable corrections."""

    def __init__(self, path: str = "continuity.db") -> None:
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self._schema()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self):
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            yield
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL UNIQUE,
              role TEXT NOT NULL CHECK(role IN ('producer','continuity','reviewer'))
            );
            CREATE TABLE IF NOT EXISTS productions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              title TEXT NOT NULL,
              description TEXT NOT NULL DEFAULT '',
              created_by INTEGER NOT NULL REFERENCES users(id),
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS scenes (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              production_id INTEGER NOT NULL REFERENCES productions(id) ON DELETE CASCADE,
              scene_number TEXT NOT NULL,
              title TEXT NOT NULL,
              narrative_order INTEGER NOT NULL CHECK(narrative_order > 0),
              UNIQUE(production_id,scene_number),
              UNIQUE(production_id,narrative_order)
            );
            CREATE TABLE IF NOT EXISTS shots (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              scene_id INTEGER NOT NULL REFERENCES scenes(id) ON DELETE CASCADE,
              shot_code TEXT NOT NULL,
              shoot_order INTEGER NOT NULL CHECK(shoot_order > 0),
              narrative_order INTEGER NOT NULL CHECK(narrative_order > 0),
              description TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL DEFAULT 'planned' CHECK(status IN ('planned','locked')),
              version INTEGER NOT NULL DEFAULT 0,
              updated_by INTEGER NOT NULL REFERENCES users(id),
              updated_at TEXT NOT NULL,
              UNIQUE(scene_id,shot_code),
              UNIQUE(scene_id,narrative_order)
            );
            CREATE TABLE IF NOT EXISTS elements (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              production_id INTEGER NOT NULL REFERENCES productions(id) ON DELETE CASCADE,
              name TEXT NOT NULL,
              kind TEXT NOT NULL CHECK(kind IN ('character','costume','prop','injury')),
              rule TEXT NOT NULL CHECK(rule IN ('stable','monotonic','allowed')),
              description TEXT NOT NULL DEFAULT '',
              UNIQUE(production_id,name)
            );
            CREATE TABLE IF NOT EXISTS element_transitions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              element_id INTEGER NOT NULL REFERENCES elements(id) ON DELETE CASCADE,
              from_state TEXT NOT NULL,
              to_state TEXT NOT NULL,
              note TEXT NOT NULL DEFAULT '',
              UNIQUE(element_id,from_state,to_state)
            );
            CREATE TABLE IF NOT EXISTS element_states (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              shot_id INTEGER NOT NULL REFERENCES shots(id) ON DELETE CASCADE,
              element_id INTEGER NOT NULL REFERENCES elements(id) ON DELETE CASCADE,
              state_value TEXT NOT NULL,
              numeric_value REAL,
              note TEXT NOT NULL DEFAULT '',
              updated_by INTEGER NOT NULL REFERENCES users(id),
              updated_at TEXT NOT NULL,
              UNIQUE(shot_id,element_id)
            );
            CREATE TABLE IF NOT EXISTS conflicts (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              scene_id INTEGER NOT NULL REFERENCES scenes(id) ON DELETE CASCADE,
              element_id INTEGER NOT NULL REFERENCES elements(id) ON DELETE CASCADE,
              from_shot_id INTEGER NOT NULL REFERENCES shots(id),
              to_shot_id INTEGER NOT NULL REFERENCES shots(id),
              kind TEXT NOT NULL,
              detail TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','exempted','resolved')),
              active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
              fingerprint TEXT NOT NULL UNIQUE,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS adjustment_plans (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              conflict_id INTEGER NOT NULL UNIQUE REFERENCES conflicts(id),
              shot_id INTEGER NOT NULL REFERENCES shots(id),
              element_id INTEGER NOT NULL REFERENCES elements(id),
              new_value TEXT NOT NULL,
              numeric_value REAL,
              reason TEXT NOT NULL,
              proposed_by INTEGER NOT NULL REFERENCES users(id),
              status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')),
              reviewed_by INTEGER REFERENCES users(id),
              review_note TEXT NOT NULL DEFAULT '',
              proposed_at TEXT NOT NULL,
              reviewed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS exemptions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              conflict_id INTEGER NOT NULL UNIQUE REFERENCES conflicts(id),
              reason TEXT NOT NULL,
              approved_by INTEGER NOT NULL REFERENCES users(id),
              approved_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sync_entries (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              device_id TEXT NOT NULL,
              seq INTEGER NOT NULL CHECK(seq >= 0),
              shot_id INTEGER NOT NULL REFERENCES shots(id),
              base_version INTEGER NOT NULL CHECK(base_version >= 0),
              kind TEXT NOT NULL CHECK(kind IN ('state','exemption')),
              payload TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','merged','review')),
              result TEXT NOT NULL DEFAULT '',
              error TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              processed_at TEXT,
              UNIQUE(device_id,seq)
            );
            """
        )
        self.conn.commit()

    def seed_demo(self) -> None:
        if self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
            return
        producer = self.add_user("制片", "producer")
        continuity = self.add_user("场记", "continuity")
        reviewer = self.add_user("审片", "reviewer")
        production = self.create_production("雨夜追踪", "非线性拍摄出的连续性示例", producer)
        scene = self.add_scene(production, "S01", "巷口相遇", 1)
        s01 = self.add_shot(scene, "S01-01", 2, 1, "角色受伤后", continuity)
        s02 = self.add_shot(scene, "S01-02", 1, 2, "角色尚未受伤", continuity)
        injury = self.add_element(production, "主角左臂伤痕", "injury", "monotonic", "伤痕严重程度只能递增")
        self.set_element_state(s01, injury, "重度", 3, "", continuity)
        self.set_element_state(s02, injury, "轻度", 1, "", continuity)
        self.check_scene(scene)

    def add_user(self, name: str, role: str) -> int:
        if not name.strip() or role not in {"producer", "continuity", "reviewer"}:
            raise DomainError("用户名或角色无效")
        with self.transaction():
            try:
                cur = self.conn.execute("INSERT INTO users(name,role) VALUES(?,?)", (name.strip(), role))
            except sqlite3.IntegrityError as exc:
                raise DomainError("用户名已存在") from exc
        return int(cur.lastrowid)

    def create_production(self, title: str, description: str, user_id: int) -> int:
        user = self.conn.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()
        if not user or user["role"] != "producer" or not title.strip():
            raise DomainError("只有制片人可以创建项目")
        with self.transaction():
            cur = self.conn.execute(
                "INSERT INTO productions(title,description,created_by,created_at) VALUES(?,?,?,?)",
                (title.strip(), description.strip(), user_id, datetime.now().isoformat()),
            )
        return int(cur.lastrowid)

    def _production_for_user(self, production_id: int, user_id: int) -> sqlite3.Row:
        user = self.conn.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()
        if not user:
            raise DomainError("用户不存在")
        if user["role"] == "reviewer":
            raise DomainError("审片人员只能审核方案和豁免，不能直接编排")
        return user

    def add_scene(self, production_id: int, scene_number: str, title: str, narrative_order: int) -> int:
        if not self.conn.execute("SELECT 1 FROM productions WHERE id=?", (production_id,)).fetchone():
            raise DomainError("项目不存在")
        if not scene_number.strip() or not title.strip() or narrative_order <= 0:
            raise DomainError("场次参数无效")
        with self.transaction():
            try:
                cur = self.conn.execute(
                    "INSERT INTO scenes(production_id,scene_number,title,narrative_order) VALUES(?,?,?,?)",
                    (production_id, scene_number.strip(), title.strip(), narrative_order),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("场次编号或叙事顺序重复") from exc
        return int(cur.lastrowid)

    def add_shot(self, scene_id: int, shot_code: str, shoot_order: int, narrative_order: int,
                 description: str, user_id: int) -> int:
        scene = self.conn.execute("SELECT production_id FROM scenes WHERE id=?", (scene_id,)).fetchone()
        if not scene:
            raise DomainError("场次不存在")
        user = self._production_for_user(scene["production_id"], user_id)
        if user["role"] not in {"producer", "continuity"}:
            raise DomainError("无权创建镜头")
        if not shot_code.strip() or shoot_order <= 0 or narrative_order <= 0:
            raise DomainError("镜头参数无效")
        with self.transaction():
            try:
                cur = self.conn.execute(
                    "INSERT INTO shots(scene_id,shot_code,shoot_order,narrative_order,description,updated_by,updated_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (scene_id, shot_code.strip(), shoot_order, narrative_order, description.strip(), user_id, datetime.now().isoformat()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("场次内镜头编号或叙事顺序重复") from exc
        return int(cur.lastrowid)

    def add_element(self, production_id: int, name: str, kind: str, rule: str, description: str = "") -> int:
        if not self.conn.execute("SELECT 1 FROM productions WHERE id=?", (production_id,)).fetchone():
            raise DomainError("项目不存在")
        if not name.strip() or kind not in ELEMENT_KINDS or rule not in RULES:
            raise DomainError("连续性元素参数无效")
        with self.transaction():
            try:
                cur = self.conn.execute(
                    "INSERT INTO elements(production_id,name,kind,rule,description) VALUES(?,?,?,?,?)",
                    (production_id, name.strip(), kind, rule, description.strip()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("项目内元素名称不能重复") from exc
        return int(cur.lastrowid)

    def add_transition(self, element_id: int, from_state: str, to_state: str, note: str = "") -> int:
        element = self.conn.execute("SELECT rule FROM elements WHERE id=?", (element_id,)).fetchone()
        if not element or element["rule"] != "allowed":
            raise DomainError("只有 allowed 规则元素需要配置状态转移")
        if not from_state.strip() or not to_state.strip() or from_state == to_state:
            raise DomainError("状态转移必须包含两个不同状态")
        with self.transaction():
            try:
                cur = self.conn.execute(
                    "INSERT INTO element_transitions(element_id,from_state,to_state,note) VALUES(?,?,?,?)",
                    (element_id, from_state.strip(), to_state.strip(), note.strip()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("该状态转移已存在") from exc
        return int(cur.lastrowid)

    def set_element_state(self, shot_id: int, element_id: int, state_value: str, numeric_value: float | None,
                          note: str, user_id: int) -> dict:
        shot = self.conn.execute("SELECT s.*,sc.production_id FROM shots s JOIN scenes sc ON sc.id=s.scene_id WHERE s.id=?", (shot_id,)).fetchone()
        element = self.conn.execute("SELECT * FROM elements WHERE id=?", (element_id,)).fetchone()
        if not shot or not element or shot["production_id"] != element["production_id"]:
            raise DomainError("镜头与元素不属于同一项目")
        user = self._production_for_user(shot["production_id"], user_id)
        if user["role"] not in {"producer", "continuity"}:
            raise DomainError("无权修改连续性状态")
        if shot["status"] == "locked":
            raise DomainError("镜头已锁定，不能直接修改状态")
        if not state_value.strip():
            raise DomainError("状态值不能为空")
        if element["rule"] == "monotonic" and numeric_value is None:
            raise DomainError("单调规则必须提供 numeric_value")
        with self.transaction():
            conflicts = self._write_element_state(shot, element, state_value, numeric_value, note, user_id)
        return {"shot_id": shot_id, "element_id": element_id, "conflicts": conflicts}

    def _write_element_state(self, shot: sqlite3.Row, element: sqlite3.Row, state_value: str,
                             numeric_value: float | None, note: str, user_id: int) -> list[dict]:
        """在已开启的事务内写入元素状态、提升镜头版本并同步冲突。"""
        now = datetime.now().isoformat()
        try:
            self.conn.execute(
                "INSERT INTO element_states(shot_id,element_id,state_value,numeric_value,note,updated_by,updated_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (shot["id"], element["id"], state_value.strip(), numeric_value, note.strip(), user_id, now),
            )
        except sqlite3.IntegrityError:
            self.conn.execute(
                "UPDATE element_states SET state_value=?,numeric_value=?,note=?,updated_by=?,updated_at=? WHERE shot_id=? AND element_id=?",
                (state_value.strip(), numeric_value, note.strip(), user_id, now, shot["id"], element["id"]),
            )
        self.conn.execute("UPDATE shots SET version=version+1,updated_by=?,updated_at=? WHERE id=?", (user_id, now, shot["id"]))
        self._sync_conflicts(shot["scene_id"])
        return self.list_conflicts(shot["scene_id"])

    def _detect_conflicts(self, scene_id: int) -> list[dict]:
        scene = self.conn.execute("SELECT * FROM scenes WHERE id=?", (scene_id,)).fetchone()
        if not scene:
            raise DomainError("场次不存在")
        shots = self.conn.execute(
            "SELECT * FROM shots WHERE scene_id=? ORDER BY narrative_order", (scene_id,)
        ).fetchall()
        elements = self.conn.execute("SELECT * FROM elements WHERE production_id=? ORDER BY id", (scene["production_id"],)).fetchall()
        detected: list[dict] = []
        for element in elements:
            sequence = []
            for shot in shots:
                state = self.conn.execute(
                    "SELECT * FROM element_states WHERE shot_id=? AND element_id=?", (shot["id"], element["id"])
                ).fetchone()
                if state:
                    sequence.append((shot, state))
            for (prev_shot, prev), (shot, current) in zip(sequence, sequence[1:]):
                kind = None
                detail = ""
                if element["rule"] == "stable":
                    if current["state_value"] != prev["state_value"]:
                        kind = "state_changed"
                        detail = f"{element['name']} 应为稳定状态，却从 {prev['state_value']} 变为 {current['state_value']}"
                elif element["rule"] == "monotonic":
                    if current["numeric_value"] is None or prev["numeric_value"] is None:
                        kind = "missing_numeric_value"
                        detail = f"{element['name']} 缺少可比较的数值"
                    elif current["numeric_value"] < prev["numeric_value"]:
                        kind = "regression"
                        detail = f"{element['name']} 在叙事顺序中从 {prev['numeric_value']} 回退到 {current['numeric_value']}"
                else:
                    allowed = self.conn.execute(
                        "SELECT 1 FROM element_transitions WHERE element_id=? AND from_state=? AND to_state=?",
                        (element["id"], prev["state_value"], current["state_value"]),
                    ).fetchone()
                    if not allowed:
                        kind = "transition_not_allowed"
                        detail = f"{element['name']} 不允许从 {prev['state_value']} 变为 {current['state_value']}"
                if kind:
                    fingerprint = f"{scene_id}:{element['id']}:{prev_shot['id']}:{shot['id']}:{kind}"
                    detected.append({
                        "scene_id": scene_id, "element_id": element["id"], "element_name": element["name"],
                        "from_shot_id": prev_shot["id"], "to_shot_id": shot["id"], "kind": kind,
                        "detail": detail, "fingerprint": fingerprint,
                    })
        return detected

    def _sync_conflicts(self, scene_id: int) -> None:
        detected = self._detect_conflicts(scene_id)
        active_fingerprints = {row["fingerprint"] for row in detected}
        for row in self.conn.execute("SELECT * FROM conflicts WHERE scene_id=? AND active=1", (scene_id,)).fetchall():
            if row["fingerprint"] not in active_fingerprints:
                self.conn.execute(
                    "UPDATE conflicts SET active=0,status='resolved',updated_at=? WHERE id=?",
                    (datetime.now().isoformat(), row["id"]),
                )
        for issue in detected:
            existing = self.conn.execute("SELECT * FROM conflicts WHERE fingerprint=?", (issue["fingerprint"],)).fetchone()
            if existing:
                status = "exempted" if existing["status"] == "exempted" else "open"
                self.conn.execute(
                    "UPDATE conflicts SET active=1,status=?,detail=?,updated_at=? WHERE id=?",
                    (status, issue["detail"], datetime.now().isoformat(), existing["id"]),
                )
            else:
                self.conn.execute(
                    "INSERT INTO conflicts(scene_id,element_id,from_shot_id,to_shot_id,kind,detail,status,active,fingerprint,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?, 'open',1,?,?,?)",
                    (issue["scene_id"], issue["element_id"], issue["from_shot_id"], issue["to_shot_id"], issue["kind"], issue["detail"], issue["fingerprint"], datetime.now().isoformat(), datetime.now().isoformat()),
                )

    def check_scene(self, scene_id: int) -> list[dict]:
        if not self.conn.execute("SELECT 1 FROM scenes WHERE id=?", (scene_id,)).fetchone():
            raise DomainError("场次不存在")
        with self.transaction():
            self._sync_conflicts(scene_id)
        return self.list_conflicts(scene_id)

    def list_conflicts(self, scene_id: int, include_resolved: bool = False) -> list[dict]:
        clause = "" if include_resolved else "AND c.active=1"
        return [dict(row) for row in self.conn.execute(
            "SELECT c.*,e.name AS element_name,fs.shot_code AS from_shot_code,ts.shot_code AS to_shot_code "
            "FROM conflicts c JOIN elements e ON e.id=c.element_id JOIN shots fs ON fs.id=c.from_shot_id JOIN shots ts ON ts.id=c.to_shot_id "
            f"WHERE c.scene_id=? {clause} ORDER BY c.id", (scene_id,)
        ).fetchall()]

    def propose_adjustment(self, conflict_id: int, new_value: str, numeric_value: float | None,
                           reason: str, user_id: int) -> int:
        conflict = self.conn.execute("SELECT * FROM conflicts WHERE id=?", (conflict_id,)).fetchone()
        if not conflict or not conflict["active"]:
            raise DomainError("冲突不存在或已解决")
        if conflict["status"] != "open":
            raise DomainError("已豁免冲突不能提交状态调整方案")
        shot = self.conn.execute("SELECT * FROM shots WHERE id=?", (conflict["to_shot_id"],)).fetchone()
        element = self.conn.execute("SELECT * FROM elements WHERE id=?", (conflict["element_id"],)).fetchone()
        user = self._production_for_user(element["production_id"], user_id)
        if user["role"] not in {"producer", "continuity"}:
            raise DomainError("无权提出调整方案")
        if shot["status"] == "locked":
            raise DomainError("目标镜头已锁定")
        if not new_value.strip() or len(reason.strip()) < 3:
            raise DomainError("新状态和调整理由必须填写")
        if element["rule"] == "monotonic" and numeric_value is None:
            raise DomainError("单调规则调整必须提供 numeric_value")
        with self.transaction():
            try:
                cur = self.conn.execute(
                    "INSERT INTO adjustment_plans(conflict_id,shot_id,element_id,new_value,numeric_value,reason,proposed_by,proposed_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (conflict_id, shot["id"], element["id"], new_value.strip(), numeric_value, reason.strip(), user_id, datetime.now().isoformat()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("该冲突已有调整方案") from exc
        return int(cur.lastrowid)

    def review_adjustment(self, plan_id: int, approve: bool, reviewer_id: int, note: str = "") -> dict:
        reviewer = self.conn.execute("SELECT role FROM users WHERE id=?", (reviewer_id,)).fetchone()
        if not reviewer or reviewer["role"] != "reviewer":
            raise DomainError("只有审片人可以审核调整方案")
        plan = self.conn.execute("SELECT * FROM adjustment_plans WHERE id=?", (plan_id,)).fetchone()
        if not plan or plan["status"] != "pending":
            raise DomainError("调整方案不存在或已审核")
        if plan["proposed_by"] == reviewer_id:
            raise DomainError("提案人不能审核自己的方案")
        shot = self.conn.execute("SELECT * FROM shots WHERE id=?", (plan["shot_id"],)).fetchone()
        if shot["status"] == "locked":
            raise DomainError("目标镜头已锁定")
        with self.transaction():
            status = "approved" if approve else "rejected"
            self.conn.execute(
                "UPDATE adjustment_plans SET status=?,reviewed_by=?,review_note=?,reviewed_at=? WHERE id=?",
                (status, reviewer_id, note.strip(), datetime.now().isoformat(), plan_id),
            )
            if approve:
                element = self.conn.execute("SELECT * FROM elements WHERE id=?", (plan["element_id"],)).fetchone()
                self._write_element_state(
                    shot, element, plan["new_value"], plan["numeric_value"],
                    f"调整方案 #{plan_id}", reviewer_id,
                )
                self.conn.execute(
                    "UPDATE conflicts SET active=0,status='resolved',updated_at=? WHERE id=?",
                    (datetime.now().isoformat(), plan["conflict_id"]),
                )
                self._sync_conflicts(shot["scene_id"])
        return {"plan_id": plan_id, "status": status, "conflicts": self.list_conflicts(shot["scene_id"])}

    def approve_exemption(self, conflict_id: int, reason: str, reviewer_id: int) -> int:
        reviewer = self.conn.execute("SELECT role FROM users WHERE id=?", (reviewer_id,)).fetchone()
        conflict = self.conn.execute("SELECT * FROM conflicts WHERE id=?", (conflict_id,)).fetchone()
        if not conflict or not conflict["active"] or not reviewer or reviewer["role"] != "reviewer":
            raise DomainError("冲突或审片人无效")
        if len(reason.strip()) < 8:
            raise DomainError("豁免理由至少8个字符")
        with self.transaction():
            exemption_id = self._write_exemption(conflict, reason, reviewer_id)
        return exemption_id

    def _write_exemption(self, conflict: sqlite3.Row, reason: str, reviewer_id: int) -> int:
        """在已开启的事务内写入豁免（UNIQUE 冲突说明已豁免）。"""
        try:
            cur = self.conn.execute(
                "INSERT INTO exemptions(conflict_id,reason,approved_by,approved_at) VALUES(?,?,?,?)",
                (conflict["id"], reason.strip(), reviewer_id, datetime.now().isoformat()),
            )
        except sqlite3.IntegrityError as exc:
            raise DomainError("该冲突已经豁免") from exc
        self.conn.execute("UPDATE conflicts SET status='exempted',updated_at=? WHERE id=?", (datetime.now().isoformat(), conflict["id"]))
        return int(cur.lastrowid)

    def lock_shot(self, shot_id: int, user_id: int) -> None:
        shot = self.conn.execute("SELECT s.*,sc.production_id FROM shots s JOIN scenes sc ON sc.id=s.scene_id WHERE s.id=?", (shot_id,)).fetchone()
        if not shot:
            raise DomainError("镜头不存在")
        user = self._production_for_user(shot["production_id"], user_id)
        if user["role"] not in {"producer", "continuity"}:
            raise DomainError("无权锁定镜头")
        with self.transaction():
            self._sync_conflicts(shot["scene_id"])
            blocking = self.conn.execute(
                "SELECT COUNT(*) FROM conflicts WHERE scene_id=? AND active=1 AND status!='exempted'", (shot["scene_id"],)
            ).fetchone()[0]
            if blocking:
                raise DomainError(f"场次仍有 {blocking} 个未处理冲突，不能锁定")
            self.conn.execute("UPDATE shots SET status='locked',version=version+1,updated_by=?,updated_at=? WHERE id=?", (user_id, datetime.now().isoformat(), shot_id))

    def continuity_report(self, production_id: int) -> dict:
        production = self.conn.execute("SELECT * FROM productions WHERE id=?", (production_id,)).fetchone()
        if not production:
            raise DomainError("项目不存在")
        scenes = []
        for scene in self.conn.execute("SELECT * FROM scenes WHERE production_id=? ORDER BY narrative_order", (production_id,)).fetchall():
            shots = [dict(r) for r in self.conn.execute("SELECT * FROM shots WHERE scene_id=? ORDER BY narrative_order", (scene["id"],))]
            conflicts = self.list_conflicts(scene["id"], include_resolved=True)
            scenes.append({**dict(scene), "shots": shots, "conflicts": conflicts})
        return {
            "production": dict(production),
            "elements": [dict(r) for r in self.conn.execute("SELECT * FROM elements WHERE production_id=? ORDER BY id", (production_id,))],
            "scenes": scenes,
            "open_conflicts": sum(1 for scene in scenes for c in scene["conflicts"] if c["active"] and c["status"] == "open"),
            "exempted_conflicts": sum(1 for scene in scenes for c in scene["conflicts"] if c["active"] and c["status"] == "exempted"),
        }

    def snapshot(self) -> dict:
        return {
            "users": [dict(r) for r in self.conn.execute("SELECT id,name,role FROM users ORDER BY id")],
            "productions": [dict(r) for r in self.conn.execute("SELECT * FROM productions ORDER BY id")],
            "scenes": [dict(r) for r in self.conn.execute("SELECT * FROM scenes ORDER BY production_id,narrative_order")],
            "shots": [dict(r) for r in self.conn.execute("SELECT * FROM shots ORDER BY scene_id,narrative_order")],
        }

    # ------------------------------------------------------------------
    # 离线回传合并：设备号 + 连续编号保证幂等，镜头版本做乐观锁
    # ------------------------------------------------------------------

    @staticmethod
    def _sync_int(value, message: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise DomainError(message)
        return value

    def _validate_sync_entry(self, entry) -> dict:
        if not isinstance(entry, dict):
            raise DomainError("回传条目必须是对象")
        kind = entry.get("kind")
        if kind not in {"state", "exemption"}:
            raise DomainError("回传条目类型必须是 state 或 exemption")
        shot_id = self._sync_int(entry.get("shot_id"), "镜头 ID 必须是整数")
        base_version = self._sync_int(entry.get("base_version"), "镜头版本必须是整数")
        if shot_id <= 0 or base_version < 0:
            raise DomainError("镜头 ID 或版本无效")
        shot = self.conn.execute("SELECT * FROM shots WHERE id=?", (shot_id,)).fetchone()
        if not shot:
            raise DomainError(f"镜头 {shot_id} 不存在")
        payload = {}
        if kind == "state":
            element_id = self._sync_int(entry.get("element_id"), "元素 ID 必须是整数")
            state_value = entry.get("state_value", "")
            numeric_value = entry.get("numeric_value")
            note = entry.get("note", "")
            user_id = self._sync_int(entry.get("user_id"), "记录人 ID 必须是整数")
            if not isinstance(state_value, str) or not state_value.strip():
                raise DomainError("状态值不能为空")
            if numeric_value is not None and (isinstance(numeric_value, bool) or not isinstance(numeric_value, (int, float))):
                raise DomainError("numeric_value 必须是数字")
            if not isinstance(note, str):
                raise DomainError("备注必须是字符串")
            element = self.conn.execute("SELECT * FROM elements WHERE id=?", (element_id,)).fetchone()
            scene = self.conn.execute("SELECT production_id FROM scenes WHERE id=?", (shot["scene_id"],)).fetchone()
            if not element or not scene or element["production_id"] != scene["production_id"]:
                raise DomainError("镜头与元素不属于同一项目")
            self._production_for_user(scene["production_id"], user_id)
            if element["rule"] == "monotonic" and numeric_value is None:
                raise DomainError("单调规则必须提供 numeric_value")
            if shot["status"] == "locked":
                raise DomainError("镜头已锁定，不能直接修改状态")
            payload = {"element_id": element_id, "state_value": state_value,
                       "numeric_value": numeric_value, "note": note, "user_id": user_id}
        else:
            conflict_id = self._sync_int(entry.get("conflict_id"), "冲突 ID 必须是整数")
            reason = entry.get("reason", "")
            reviewer_id = self._sync_int(entry.get("reviewer_id"), "审片人 ID 必须是整数")
            if not isinstance(reason, str) or len(reason.strip()) < 8:
                raise DomainError("豁免理由至少8个字符")
            reviewer = self.conn.execute("SELECT role FROM users WHERE id=?", (reviewer_id,)).fetchone()
            if not reviewer or reviewer["role"] != "reviewer":
                raise DomainError("冲突或审片人无效")
            conflict = self.conn.execute("SELECT * FROM conflicts WHERE id=?", (conflict_id,)).fetchone()
            if not conflict or not conflict["active"]:
                raise DomainError("冲突不存在或已解决")
            if conflict["to_shot_id"] != shot_id:
                raise DomainError("豁免条目与镜头不匹配")
            payload = {"conflict_id": conflict_id, "reason": reason, "reviewer_id": reviewer_id}
        return {"kind": kind, "shot_id": shot_id, "base_version": base_version,
                "payload": payload, "shot": shot}

    def sync_batch(self, device_id: str, entries: list) -> dict:
        device_id = str(device_id or "").strip()
        if not device_id:
            raise DomainError("设备号不能为空")
        if not isinstance(entries, list) or not entries:
            raise DomainError("回传条目不能为空")
        seqs: set[int] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                raise DomainError("回传条目必须是对象")
            seq = self._sync_int(entry.get("seq"), "连续编号必须是整数")
            if seq < 0 or seq in seqs:
                raise DomainError("同批次连续编号缺失或重复")
            seqs.add(seq)
        prepared = [self._validate_sync_entry(e) for e in entries]
        now = datetime.now().isoformat()
        with self.transaction():
            for entry, valid in zip(entries, prepared):
                try:
                    self.conn.execute(
                        "INSERT INTO sync_entries(device_id,seq,shot_id,base_version,kind,payload,created_at) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (device_id, entry["seq"], valid["shot_id"], valid["base_version"],
                         valid["kind"], json.dumps(valid["payload"], ensure_ascii=False), now),
                    )
                except sqlite3.IntegrityError:
                    # 同设备同编号重复回传：沿用原条目，不覆盖任何内容
                    pass
        # 条目已逐条落盘，处理时每条独立事务，中断后可从 pending 条目续走
        blocked = self._process_device(device_id)
        return {"device_id": device_id, "results": self._device_entries(device_id),
                "blocked_seq": blocked, **self._device_progress(device_id)}

    def _device_entries(self, device_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM sync_entries WHERE device_id=? ORDER BY seq", (device_id,)
        ).fetchall()
        return [self._entry_dict(r) for r in rows]

    def _process_device(self, device_id: str) -> int | None:
        """按连续编号处理该设备所有待处理条目；条目级独立事务，失败即停在该处等待续传。"""
        while True:
            row = self.conn.execute(
                "SELECT * FROM sync_entries WHERE device_id=? AND status='pending' ORDER BY seq LIMIT 1",
                (device_id,),
            ).fetchone()
            if not row:
                return None
            try:
                outcome = self._merge_entry(row)
            except DomainError as exc:
                with self.transaction():
                    self.conn.execute("UPDATE sync_entries SET error=? WHERE id=?", (str(exc), row["id"]))
                return row["seq"]
            with self.transaction():
                self.conn.execute(
                    "UPDATE sync_entries SET status=?,result=?,error='',processed_at=? WHERE id=?",
                    (outcome["status"], json.dumps(outcome, ensure_ascii=False),
                     datetime.now().isoformat(), row["id"]),
                )

    def _merge_entry(self, row: sqlite3.Row) -> dict:
        shot = self.conn.execute("SELECT * FROM shots WHERE id=?", (row["shot_id"],)).fetchone()
        payload = json.loads(row["payload"])
        if not shot:
            return {"status": "review", "reason": "shot_missing",
                    "message": "镜头已不存在，留待人工核对"}
        if shot["version"] != row["base_version"]:
            # 棚内已改动该镜头：绝不回写，留为待核对
            return {"status": "review", "reason": "version_mismatch",
                    "message": f"镜头版本已从 {row['base_version']} 变为 {shot['version']}，棚内已有改动",
                    "server_version": shot["version"]}
        if row["kind"] == "state":
            return self._merge_state_entry(row, shot, payload)
        return self._merge_exemption_entry(row, shot, payload)

    def _merge_state_entry(self, row: sqlite3.Row, shot: sqlite3.Row, payload: dict) -> dict:
        element = self.conn.execute("SELECT * FROM elements WHERE id=?", (payload["element_id"],)).fetchone()
        if not element:
            return {"status": "review", "reason": "element_missing", "message": "连续性元素已不存在，留待人工核对"}
        if shot["status"] == "locked":
            return {"status": "review", "reason": "shot_locked", "message": "镜头已锁定，任何离线写入都留待人工核对"}
        with self.transaction():
            # 事务内复查版本，防止与棚内修改并发互相覆盖
            current = self.conn.execute("SELECT version FROM shots WHERE id=?", (shot["id"],)).fetchone()
            if current["version"] != row["base_version"]:
                return {"status": "review", "reason": "version_mismatch",
                        "message": f"镜头版本已从 {row['base_version']} 变为 {current['version']}，棚内已有改动",
                        "server_version": current["version"]}
            conflicts = self._write_element_state(
                shot, element, payload["state_value"], payload["numeric_value"],
                payload["note"], payload["user_id"],
            )
        return {"status": "merged", "kind": "state", "shot_id": shot["id"],
                "shot_code": shot["shot_code"], "element_id": element["id"],
                "applied_version": row["base_version"] + 1,
                "open_conflicts": sum(1 for c in conflicts if c["active"] and c["status"] == "open")}

    def _merge_exemption_entry(self, row: sqlite3.Row, shot: sqlite3.Row, payload: dict) -> dict:
        conflict = self.conn.execute("SELECT * FROM conflicts WHERE id=?", (payload["conflict_id"],)).fetchone()
        if not conflict or not conflict["active"]:
            return {"status": "review", "reason": "conflict_missing",
                    "message": "冲突不存在或已解决，留待人工核对"}
        if conflict["status"] != "open":
            return {"status": "review", "reason": "conflict_not_open",
                    "message": f"冲突当前状态为 {conflict['status']}，不能再写入豁免"}
        if conflict["to_shot_id"] != shot["id"]:
            return {"status": "review", "reason": "shot_mismatch", "message": "豁免条目与镜头不匹配"}
        with self.transaction():
            current = self.conn.execute("SELECT version FROM shots WHERE id=?", (shot["id"],)).fetchone()
            if current["version"] != row["base_version"]:
                return {"status": "review", "reason": "version_mismatch",
                        "message": f"镜头版本已从 {row['base_version']} 变为 {current['version']}，棚内已有改动",
                        "server_version": current["version"]}
            exemption_id = self._write_exemption(conflict, payload["reason"], payload["reviewer_id"])
        return {"status": "merged", "kind": "exemption", "shot_id": shot["id"],
                "shot_code": shot["shot_code"], "conflict_id": conflict["id"],
                "exemption_id": exemption_id, "applied_version": shot["version"]}

    def resume_sync(self, device_id: str | None = None) -> dict:
        """中断后重开服务时调用：从未完成条目接着处理。"""
        if device_id is not None:
            device_id = str(device_id).strip()
            if not device_id:
                raise DomainError("设备号不能为空")
            device_ids = [device_id]
        else:
            device_ids = [r["device_id"] for r in self.conn.execute(
                "SELECT DISTINCT device_id FROM sync_entries WHERE status='pending' ORDER BY device_id"
            ).fetchall()]
        devices = []
        for dev in device_ids:
            blocked = self._process_device(dev)
            devices.append({"device_id": dev, "blocked_seq": blocked, **self._device_progress(dev)})
        return {"devices": devices}

    def _device_progress(self, device_id: str) -> dict:
        stats = self.conn.execute(
            "SELECT status,COUNT(*) AS n,COALESCE(MAX(seq),-1) AS max_seq FROM sync_entries WHERE device_id=? GROUP BY status",
            (device_id,),
        ).fetchall()
        by_status = {r["status"]: r["n"] for r in stats}
        total = sum(by_status.values())
        last_seq = max((r["max_seq"] for r in stats), default=-1)
        return {"total": total, "pending": by_status.get("pending", 0),
                "merged": by_status.get("merged", 0), "review": by_status.get("review", 0),
                "last_seq": last_seq}

    def sync_progress(self) -> dict:
        rows = self.conn.execute(
            "SELECT device_id,status,COUNT(*) AS n,COALESCE(MAX(seq),-1) AS max_seq "
            "FROM sync_entries GROUP BY device_id,status ORDER BY device_id"
        ).fetchall()
        devices: dict[str, dict] = {}
        for row in rows:
            dev = devices.setdefault(row["device_id"], {"device_id": row["device_id"], "total": 0,
                                       "pending": 0, "merged": 0, "review": 0, "last_seq": -1})
            dev[row["status"]] = row["n"]
            dev["total"] += row["n"]
            dev["last_seq"] = max(dev["last_seq"], row["max_seq"])
        blocked = self.conn.execute(
            "SELECT device_id,MIN(seq) AS seq FROM sync_entries WHERE status='pending' GROUP BY device_id"
        ).fetchall()
        blocked_map = {r["device_id"]: r["seq"] for r in blocked}
        for dev in devices.values():
            dev["blocked_seq"] = blocked_map.get(dev["device_id"])
        return {"devices": sorted(devices.values(), key=lambda d: d["device_id"])}

    def _entry_dict(self, row: sqlite3.Row) -> dict:
        shot = self.conn.execute(
            "SELECT s.shot_code,s.scene_id,sc.scene_number FROM shots s "
            "JOIN scenes sc ON sc.id=s.scene_id WHERE s.id=?", (row["shot_id"],)
        ).fetchone()
        result = json.loads(row["result"]) if row["result"] else None
        return {
            "id": row["id"], "device_id": row["device_id"], "seq": row["seq"],
            "shot_id": row["shot_id"], "shot_code": shot["shot_code"] if shot else None,
            "scene_number": shot["scene_number"] if shot else None,
            "base_version": row["base_version"], "kind": row["kind"],
            "payload": json.loads(row["payload"]), "status": row["status"],
            "result": result, "error": row["error"],
            "created_at": row["created_at"], "processed_at": row["processed_at"],
        }

    def list_sync_entries(self, device_id: str | None = None, status_filter: str | None = None) -> dict:
        sql = "SELECT * FROM sync_entries"
        where, params = [], []
        if device_id:
            where.append("device_id=?"); params.append(device_id)
        if status_filter:
            where.append("status=?"); params.append(status_filter)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY device_id,seq"
        entries = [self._entry_dict(r) for r in self.conn.execute(sql, params).fetchall()]
        return {"entries": entries, **self.sync_progress()}
