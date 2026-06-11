import os, httpx
BASE = os.environ["FRONTEND_URL"].rstrip("/")
def test_health():
    assert httpx.get(f"{BASE}/healthz", timeout=20).status_code == 200
def test_move_updates_board_and_status():  # AC#3
    r = httpx.post(f"{BASE}/move", json={"board":["X","O","X","O","X","",""," ".strip(),""],"cell":8,"player":"X"}, timeout=20)
    assert r.status_code == 200
    d = r.json()
    assert d["board"][8] == "X"
    assert d["status"] in ("win:X","draw","in-progress")
def test_win_detected():  # AC#3
    r = httpx.post(f"{BASE}/move", json={"board":["X","X","","","","","","",""],"cell":2,"player":"X"}, timeout=20)
    assert r.json()["status"] == "win:X"
