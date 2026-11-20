
import hashlib, secrets, sqlite3
from pathlib import Path
from fastapi import Header, HTTPException
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DB_PATH = DATA / "app.db"
FRONTEND = ROOT / "frontend"
SESSIONS: dict[str, str] = {}

def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def _ensure_column(conn, table: str, column: str, ddl: str):
    cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")

def sha(p: str) -> str:
    return hashlib.sha256(p.encode("utf-8")).hexdigest()

def ensure_users(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS users(
        username TEXT PRIMARY KEY, password_sha TEXT NOT NULL, role TEXT NOT NULL)""")
    if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0:
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("admin", sha("123456"), "管理员"))
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("viewer", sha("view123"), "只读"))

class LoginIn(BaseModel):
    username: str
    password: str

def require_user(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "未登录")
    u = SESSIONS.get(authorization.removeprefix("Bearer ").strip())
    if not u:
        raise HTTPException(401, "登录已失效")
    return u

from datetime import datetime
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

PRODUCT = "实体键桥"
SKELETON = "实体键映射生效后入站先重映再进正式区，冲突进队列，映射版本与正式区对账"

app = FastAPI(title=PRODUCT)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

def init_db():
    with connect() as conn:
        ensure_users(conn)
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS map_versions(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          version TEXT UNIQUE NOT NULL,
          status TEXT NOT NULL,
          effective_from TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS mappings(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          version TEXT NOT NULL,
          old_key TEXT NOT NULL,
          new_key TEXT NOT NULL,
          kind TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS formal_entities(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          entity_key TEXT NOT NULL,
          mapped_from TEXT NOT NULL,
          version TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS conflicts(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          old_key TEXT NOT NULL,
          reason TEXT NOT NULL,
          status TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS retirement_ledger(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          mapping_id INTEGER UNIQUE NOT NULL,
          old_key TEXT NOT NULL,
          new_key TEXT NOT NULL,
          retired_at TEXT NOT NULL,
          formal_watermark INTEGER NOT NULL,
          operator TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS remap_rejections(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          old_key TEXT NOT NULL,
          mapping_id INTEGER,
          reason TEXT NOT NULL,
          outcome TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS map_row_locks(
          lock_key TEXT PRIMARY KEY,
          holder TEXT NOT NULL,
          acquired_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS lock_contention(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          lock_key TEXT NOT NULL,
          old_key TEXT NOT NULL,
          holder TEXT NOT NULL,
          contender TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        """)
        _ensure_column(conn, "mappings", "status", "status TEXT NOT NULL DEFAULT '生效'")
        if conn.execute("SELECT COUNT(*) c FROM map_versions").fetchone()["c"] == 0:
            conn.execute("INSERT INTO map_versions(version,status,effective_from) VALUES (?,?,?)", ("m-2026.11","生效","2026-11-01"))
            conn.executemany("INSERT INTO mappings(version,old_key,new_key,kind) VALUES (?,?,?,?)", [
                ("m-2026.11","OLD-A1","NEW-100","一对一"),
                ("m-2026.11","OLD-B1","NEW-200","多对一"),
                ("m-2026.11","OLD-B2","NEW-200","多对一"),
            ])
            conn.execute("INSERT INTO formal_entities(entity_key,mapped_from,version,created_at) VALUES (?,?,?,?)",
                         ("NEW-100","OLD-A1","m-2026.11","2026-11-10T09:00:00"))
            conn.execute("INSERT INTO conflicts(old_key,reason,status) VALUES (?,?,?)", ("OLD-C9","无映射","挂起"))

@app.on_event("startup")
def _s(): init_db()

def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")

def map_lock_key(mapping_id: int) -> str:
    return f"mapping:{mapping_id}"

def acquire_map_lock(conn, lock_key: str, old_key: str, holder: str) -> bool:
    """抢占 map_row 行锁；已被占则记争用并返回 False。锁的取得即时提交，保证跨连接可见。"""
    try:
        conn.execute("INSERT INTO map_row_locks(lock_key,holder,acquired_at) VALUES (?,?,?)",
                     (lock_key, holder, _now()))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        conn.rollback()
        record_contention(conn, lock_key, old_key, holder)
        return False

def record_contention(conn, lock_key: str, old_key: str, contender: str):
    row = conn.execute("SELECT holder FROM map_row_locks WHERE lock_key=?", (lock_key,)).fetchone()
    conn.execute("INSERT INTO lock_contention(lock_key,old_key,holder,contender,created_at) VALUES (?,?,?,?,?)",
                 (lock_key, old_key, row["holder"] if row else "unknown", contender, _now()))
    conn.commit()

def release_map_lock(conn, lock_key: str, holder: str):
    conn.execute("DELETE FROM map_row_locks WHERE lock_key=? AND holder=?", (lock_key, holder))
    conn.commit()

@app.get("/api/health")
def health():
    with connect() as c:
        n = c.execute("SELECT COUNT(*) c FROM mappings").fetchone()["c"]
    return {"ok": True, "product": PRODUCT, "skeleton": SKELETON, "mappings": n, "db": str(DB_PATH)}

@app.post("/api/login")
def login(body: LoginIn):
    with connect() as c:
        row = c.execute("SELECT username,role FROM users WHERE username=? AND password_sha=?", (body.username, sha(body.password))).fetchone()
    if not row: raise HTTPException(401, "账号或密码错误")
    tok = secrets.token_hex(16); SESSIONS[tok]=row["username"]
    return {"token": tok, "username": row["username"], "role": row["role"]}

@app.get("/api/map-versions")
def versions(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM map_versions ORDER BY id").fetchall()]}

@app.get("/api/mappings")
def mappings(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM mappings ORDER BY id").fetchall()]}

class RetireIn(BaseModel):
    reason: str | None = None

@app.post("/api/mappings/{mapping_id}/retire")
def retire(mapping_id: int, body: RetireIn | None = None, authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    with connect() as c:
        m = c.execute("SELECT * FROM mappings WHERE id=?", (mapping_id,)).fetchone()
        if not m: raise HTTPException(404, "映射不存在")
        lock_key = map_lock_key(mapping_id)
        if not acquire_map_lock(c, lock_key, m["old_key"], f"retire:{user}"):
            raise HTTPException(409, "映射行争用，退役中止（已记争用）")
        try:
            cur = c.execute("SELECT * FROM mappings WHERE id=?", (mapping_id,)).fetchone()
            if cur["status"] == "已退役":
                raise HTTPException(409, "该映射已退役")
            wm = c.execute("SELECT COALESCE(MAX(id),0) w FROM formal_entities").fetchone()["w"]
            c.execute("UPDATE mappings SET status='已退役' WHERE id=?", (mapping_id,))
            c.execute("INSERT INTO retirement_ledger(mapping_id,old_key,new_key,retired_at,formal_watermark,operator) VALUES (?,?,?,?,?,?)",
                      (mapping_id, cur["old_key"], cur["new_key"], _now(), wm, user))
            c.commit()
            return {"mapping_id": mapping_id, "old_key": cur["old_key"], "status": "已退役", "formal_watermark": wm}
        except HTTPException:
            c.rollback()
            raise
        except Exception:
            c.rollback()
            raise HTTPException(500, "退役失败，已回滚")
        finally:
            release_map_lock(c, lock_key, f"retire:{user}")

class RemapIn(BaseModel):
    old_key: str

@app.post("/api/remap")
def remap(body: RemapIn, authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    now = datetime.now().isoformat(timespec="seconds")
    with connect() as c:
        ver = c.execute("SELECT version FROM map_versions WHERE status='生效' ORDER BY id DESC LIMIT 1").fetchone()
        if not ver: raise HTTPException(404, "无生效映射版本")
        m = c.execute("SELECT * FROM mappings WHERE version=? AND old_key=?", (ver["version"], body.old_key)).fetchone()
        if not m:
            c.execute("INSERT INTO conflicts(old_key,reason,status) VALUES (?,?,?)", (body.old_key, "无映射", "挂起"))
            c.commit()
            raise HTTPException(409, "无映射，已进冲突队列")

        lock_key = map_lock_key(m["id"])
        if not acquire_map_lock(c, lock_key, body.old_key, f"remap:{user}"):
            raise HTTPException(409, "映射行争用，重映中止（已记争用）")
        try:
            cur = c.execute("SELECT status FROM mappings WHERE id=?", (m["id"],)).fetchone()
            if cur["status"] == "已退役":
                c.execute("INSERT INTO remap_rejections(old_key,mapping_id,reason,outcome,created_at) VALUES (?,?,?,?,?)",
                          (body.old_key, m["id"], "映射已退役，拒绝重映", "拒映", now))
                c.commit()
                raise HTTPException(409, "映射已退役，拒绝重映（已记拒映审计）")
            c.execute("INSERT INTO formal_entities(entity_key,mapped_from,version,created_at) VALUES (?,?,?,?)",
                      (m["new_key"], body.old_key, ver["version"], now))
            c.commit()
            return {"entity_key": m["new_key"], "version": ver["version"]}
        except HTTPException:
            raise
        except Exception:
            c.rollback()
            raise HTTPException(500, "重映失败，已回滚")
        finally:
            release_map_lock(c, lock_key, f"remap:{user}")

@app.get("/api/formal")
def formal(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM formal_entities ORDER BY id DESC").fetchall()]}

@app.get("/api/conflicts")
def conflicts(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM conflicts ORDER BY id DESC").fetchall()]}

@app.get("/api/retirement-ledger")
def retirement_ledger(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM retirement_ledger ORDER BY id DESC").fetchall()]}

@app.get("/api/remap-rejections")
def remap_rejections(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM remap_rejections ORDER BY id DESC").fetchall()]}

@app.get("/api/lock-contention")
def lock_contention(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM lock_contention ORDER BY id DESC").fetchall()]}

@app.get("/api/attestation")
def attestation(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        maps = {r["id"]: dict(r) for r in c.execute("SELECT * FROM mappings").fetchall()}
        ledger = [dict(r) for r in c.execute("SELECT * FROM retirement_ledger ORDER BY id").fetchall()]
        rejections = [dict(r) for r in c.execute("SELECT * FROM remap_rejections ORDER BY id").fetchall()]
        checks = []

        ledger_ids = {r["mapping_id"] for r in ledger}
        missing = [mid for mid, m in maps.items() if m["status"] == "已退役" and mid not in ledger_ids]
        checks.append({"check": "退役台账齐全", "ok": not missing, "detail": missing})

        badge_mismatch = [r["mapping_id"] for r in ledger if maps.get(r["mapping_id"], {}).get("status") != "已退役"]
        checks.append({"check": "映射册徽章同态", "ok": not badge_mismatch, "detail": badge_mismatch})

        alive = []
        for r in ledger:
            n = c.execute("SELECT COUNT(*) c FROM formal_entities WHERE mapped_from=? AND id>?",
                          (r["old_key"], r["formal_watermark"])).fetchone()["c"]
            if n: alive.append({"mapping_id": r["mapping_id"], "old_key": r["old_key"], "new_rows": n})
        checks.append({"check": "退役后正式零增", "ok": not alive, "detail": alive})

        bad_rej = [r["id"] for r in rejections if r["outcome"] != "拒映"]
        checks.append({"check": "拒映审计同态", "ok": not bad_rej, "detail": bad_rej})

        return {"consistent": all(x["ok"] for x in checks), "checks": checks,
                "counts": {"mappings": len(maps), "retired": len(ledger),
                           "rejections": len(rejections),
                           "lock_contention": c.execute("SELECT COUNT(*) c FROM lock_contention").fetchone()["c"]}}

@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")
app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")
