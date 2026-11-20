
import pytest
from fastapi.testclient import TestClient
import app as appmod
from app import app, init_db

@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("app.DB_PATH", tmp_path/"t.db"); init_db(); yield

def _auth(c):
    tok = c.post("/api/login", json={"username":"admin","password":"123456"}).json()["token"]
    return {"Authorization":"Bearer "+tok}

def _mapping_id(old_key):
    with appmod.connect() as c:
        return c.execute("SELECT id FROM mappings WHERE old_key=?", (old_key,)).fetchone()["id"]

def _formal_count(old_key=None):
    with appmod.connect() as c:
        if old_key is None:
            return c.execute("SELECT COUNT(*) n FROM formal_entities").fetchone()["n"]
        return c.execute("SELECT COUNT(*) n FROM formal_entities WHERE mapped_from=?", (old_key,)).fetchone()["n"]

def test_retired_mapping_must_not_remap_into_formal():
    """红测一：退役后重映不得写入正式（甲路径：整笔拒绝 + 拒映审计）。"""
    c = TestClient(app)
    h = _auth(c)
    mid = _mapping_id("OLD-A1")

    r = c.post(f"/api/mappings/{mid}/retire", headers=h, json={"reason":"下线"})
    assert r.status_code == 200, r.text
    before = _formal_count()

    r = c.post("/api/remap", headers=h, json={"old_key":"OLD-A1"})

    assert r.status_code == 409, f"退役后重映必须整笔拒绝，实际 {r.status_code} {r.text}"
    assert _formal_count() == before, "退役后正式区不得新增"
    rej = c.get("/api/remap-rejections", headers=h).json()["items"]
    assert any(x["old_key"] == "OLD-A1" and x["outcome"] == "拒映" for x in rej), "必须写拒映审计"
    assert c.get("/api/attestation", headers=h).json()["consistent"] is True

def test_retire_and_remap_contend_on_map_row_lock():
    """红测二：退役/重映并发须占 map_row 锁，后到只记争用、不落正式、不改徽章。"""
    c = TestClient(app)
    h = _auth(c)
    mid = _mapping_id("OLD-A1")
    lock_key = f"mapping:{mid}"

    # 模拟退役事务已持有该映射行锁
    with appmod.connect() as conn:
        conn.execute("INSERT INTO map_row_locks(lock_key,holder,acquired_at) VALUES (?,?,?)",
                     (lock_key, "retire-holder", "2026-11-20T00:00:00"))
        conn.commit()

    before = _formal_count()
    r = c.post("/api/remap", headers=h, json={"old_key":"OLD-A1"})
    assert r.status_code == 409, f"锁被占时重映须中止，实际 {r.status_code} {r.text}"
    assert _formal_count() == before, "争用败者不得落正式"

    r = c.post(f"/api/mappings/{mid}/retire", headers=h, json={"reason":"下线"})
    assert r.status_code == 409, f"锁被占时退役须中止，实际 {r.status_code} {r.text}"
    with appmod.connect() as conn:
        st = conn.execute("SELECT status FROM mappings WHERE id=?", (mid,)).fetchone()["status"]
    assert st != "已退役", "争用败者不得改徽章"

    cont = c.get("/api/lock-contention", headers=h).json()["items"]
    assert sum(1 for x in cont if x["lock_key"] == lock_key) >= 2, "退役与重映两路后到均须记争用"

def test_map_row_lock_released_after_writes():
    """写路径结束后必须释放 map_row 锁，不得把后续正常写入锁死。"""
    c = TestClient(app)
    h = _auth(c)
    mid = _mapping_id("OLD-B1")

    assert c.post("/api/remap", headers=h, json={"old_key":"OLD-B1"}).status_code == 200
    assert c.post(f"/api/mappings/{mid}/retire", headers=h, json={"reason":"下线"}).status_code == 200
    with appmod.connect() as conn:
        assert conn.execute("SELECT COUNT(*) n FROM map_row_locks").fetchone()["n"] == 0
    assert c.get("/api/attestation", headers=h).json()["consistent"] is True
