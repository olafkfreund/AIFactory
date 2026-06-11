import os
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
app = FastAPI(title="scoreboard")
TOKEN = os.environ.get("SCORE_TOKEN","secret-token")
_scores = {}

class Result(BaseModel):
    player: str
    won: bool

@app.get("/healthz")
def healthz(): return {"ok": True}

@app.post("/scores", status_code=201)
def post_score(r: Result, authorization: str = Header(default="")):
    if authorization != f"Bearer {TOKEN}":
        raise HTTPException(status_code=401, detail="unauthorized")
    s = _scores.setdefault(r.player, {"player": r.player, "wins": 0, "games": 0})
    s["games"] += 1
    if r.won: s["wins"] += 1
    return s

@app.get("/scores")
def get_scores():
    return sorted(_scores.values(), key=lambda s: s["wins"], reverse=True)
