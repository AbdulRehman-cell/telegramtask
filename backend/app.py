# backend/app.py
from fastapi import FastAPI
from db import init_db, mark_done

app = FastAPI()

# Initialize database when app starts
init_db()


@app.get("/health")
async def health():
    return {"status": "ok"}


# Placeholder endpoint that a processor/worker could call to notify job completion
@app.post("/job-complete")
async def job_complete(job_id: int, result_path: str):
    mark_done(job_id, result_path)
    return {"ok": True}
