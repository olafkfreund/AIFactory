import os, httpx
BASE = os.environ["SCOREBOARD_URL"].rstrip("/")
TOK = os.environ.get("SCORE_TOKEN","secret-token")
def test_post_requires_auth():  # AC#4 — unauthorized rejected
    r = httpx.post(f"{BASE}/scores", json={"player":"alice","won":True}, timeout=20)
    assert r.status_code == 401
def test_post_with_token_creates():  # AC#4 — authorized accepted
    r = httpx.post(f"{BASE}/scores", json={"player":"alice","won":True},
                   headers={"Authorization": f"Bearer {TOK}"}, timeout=20)
    assert r.status_code == 201
