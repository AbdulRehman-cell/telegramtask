# backend/tasks.py
import time
from db import fetch_next_job, mark_done, mark_failed


# This is a mock worker that "processes" jobs by sleeping then marking done.
def run_once():
    job = fetch_next_job()
    if not job:
        return False
    try:
        print("Processing job", job["id"])
        # Simulate processing time
        time.sleep(5)
        result_path = f"results/report_{job['id']}.pdf"
        mark_done(job["id"], result_path)
        print("Job completed:", job["id"])
        return True
    except Exception as e:
        print("Job failed:", e)
        mark_failed(job["id"], str(e))
        return False
