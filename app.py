from __future__ import annotations
import json, os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from database import ContinuityDB, DomainError

BASE=Path(__file__).resolve().parent; DB_PATH=os.environ.get("CONTINUITY_DB",str(BASE/"continuity.db"))
class Handler(BaseHTTPRequestHandler):
    db=ContinuityDB(DB_PATH)
    def log_message(self,fmt,*args): return
    def _json(self,status,payload):
        data=json.dumps(payload,ensure_ascii=False).encode(); self.send_response(status); self.send_header("Content-Type","application/json; charset=utf-8"); self.send_header("Content-Length",str(len(data))); self.end_headers(); self.wfile.write(data)
    def _body(self):
        n=int(self.headers.get("Content-Length",0))
        try: b=json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError as exc: raise DomainError("请求体必须是 JSON") from exc
        if not isinstance(b,dict): raise DomainError("请求体必须是对象")
        return b
    def do_GET(self):
        p=urlparse(self.path); parts=[x for x in p.path.split("/") if x]
        try:
            if p.path in ("/","/index.html"):
                data=(BASE/"static"/"index.html").read_bytes(); self.send_response(200); self.send_header("Content-Type","text/html; charset=utf-8"); self.send_header("Content-Length",str(len(data))); self.end_headers(); self.wfile.write(data); return
            if p.path=="/api/state": return self._json(200,self.db.snapshot())
            if len(parts)==4 and parts[:2]==["api","productions"] and parts[3]=="continuity": return self._json(200,self.db.continuity_report(int(parts[2])))
            if len(parts)==4 and parts[:2]==["api","scenes"] and parts[3]=="conflicts": return self._json(200,{"conflicts":self.db.list_conflicts(int(parts[2]),True)})
            self._json(404,{"ok":False,"error":"接口不存在"})
        except (DomainError,ValueError) as exc: self._json(400,{"ok":False,"error":str(exc)})
    def do_POST(self):
        parts=[x for x in urlparse(self.path).path.split("/") if x]; path="/"+"/".join(parts)
        try:
            b=self._body()
            if path=="/api/users": return self._json(201,{"ok":True,"id":self.db.add_user(str(b.get("name","")),str(b.get("role","continuity")))})
            if path=="/api/productions": return self._json(201,{"ok":True,"id":self.db.create_production(str(b.get("title","")),str(b.get("description","")),int(b.get("user_id",0)))})
            if len(parts)==4 and parts[:2]==["api","productions"] and parts[3]=="scenes": return self._json(201,{"ok":True,"id":self.db.add_scene(int(parts[2]),str(b.get("scene_number","")),str(b.get("title","")),int(b.get("narrative_order",0)))})
            if len(parts)==4 and parts[:2]==["api","productions"] and parts[3]=="elements": return self._json(201,{"ok":True,"id":self.db.add_element(int(parts[2]),str(b.get("name","")),str(b.get("kind","prop")),str(b.get("rule","stable")),str(b.get("description","")))})
            if len(parts)==4 and parts[:2]==["api","scenes"] and parts[3]=="shots": return self._json(201,{"ok":True,"id":self.db.add_shot(int(parts[2]),str(b.get("shot_code","")),int(b.get("shoot_order",0)),int(b.get("narrative_order",0)),str(b.get("description","")),int(b.get("user_id",0)))})
            if len(parts)==4 and parts[:2]==["api","scenes"] and parts[3]=="check": return self._json(200,{"ok":True,"conflicts":self.db.check_scene(int(parts[2]))})
            if len(parts)==4 and parts[:2]==["api","shots"] and parts[3]=="states": return self._json(200,{"ok":True,**self.db.set_element_state(int(parts[2]),int(b.get("element_id",0)),str(b.get("state_value","")),b.get("numeric_value"),str(b.get("note","")),int(b.get("user_id",0)))})
            if len(parts)==4 and parts[:2]==["api","shots"] and parts[3]=="lock": self.db.lock_shot(int(parts[2]),int(b.get("user_id",0))); return self._json(200,{"ok":True})
            if len(parts)==4 and parts[:2]==["api","elements"] and parts[3]=="transitions": return self._json(201,{"ok":True,"id":self.db.add_transition(int(parts[2]),str(b.get("from_state","")),str(b.get("to_state","")),str(b.get("note","")))})
            if len(parts)==4 and parts[:2]==["api","conflicts"] and parts[3]=="plans": return self._json(201,{"ok":True,"id":self.db.propose_adjustment(int(parts[2]),str(b.get("new_value","")),b.get("numeric_value"),str(b.get("reason","")),int(b.get("user_id",0)))})
            if len(parts)==4 and parts[:2]==["api","conflicts"] and parts[3]=="exemptions": return self._json(201,{"ok":True,"id":self.db.approve_exemption(int(parts[2]),str(b.get("reason","")),int(b.get("reviewer_id",0)))})
            if len(parts)==4 and parts[:2]==["api","plans"] and parts[3]=="review": return self._json(200,{"ok":True,**self.db.review_adjustment(int(parts[2]),bool(b.get("approve",False)),int(b.get("reviewer_id",0)),str(b.get("note","")))})
            self._json(404,{"ok":False,"error":"接口不存在"})
        except (DomainError,ValueError) as exc: self._json(400,{"ok":False,"error":str(exc)})
def main():
    ContinuityDB(DB_PATH).seed_demo(); port=int(os.environ.get("PORT","8115")); print(f"Film continuity service: http://127.0.0.1:{port}"); ThreadingHTTPServer(("0.0.0.0",port),Handler).serve_forever()
if __name__=="__main__": main()
