import os, httpx
BASE = os.environ["SCOREBOARD_URL"].rstrip("/")
TOK = os.environ.get("SCORE_TOKEN","secret-token")
H = {"Authorization": f"Bearer {TOK}"}
def test_leaderboard_sorted_by_wins():  # AC#5
    for _ in range(3): httpx.post(f"{BASE}/scores", json={"player":"bob","won":True}, headers=H, timeout=20)
    httpx.post(f"{BASE}/scores", json={"player":"cara","won":True}, headers=H, timeout=20)
    rows = httpx.get(f"{BASE}/scores", timeout=20).json()
    wins = [r["wins"] for r in rows]
    assert wins == sorted(wins, reverse=True)
