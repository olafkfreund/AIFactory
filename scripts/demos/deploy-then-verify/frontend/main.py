from fastapi import FastAPI
from pydantic import BaseModel
app = FastAPI(title="frontend")

WIN_LINES = [(0,1,2),(3,4,5),(6,7,8),(0,3,6),(1,4,7),(2,5,8),(0,4,8),(2,4,6)]

class Move(BaseModel):
    board: list[str]   # 9 cells: "X","O",""
    cell: int
    player: str

def status(b):
    for a,c,d in WIN_LINES:
        if b[a] and b[a]==b[c]==b[d]: return f"win:{b[a]}"
    return "draw" if all(b) else "in-progress"

@app.get("/healthz")
def healthz(): return {"ok": True}

@app.get("/")
def root(): return {"service":"frontend","game":"tic-tac-toe"}

@app.post("/move")
def move(m: Move):
    b = list(m.board)
    if 0 <= m.cell < 9 and not b[m.cell]:
        b[m.cell] = m.player
    return {"board": b, "status": status(b)}
