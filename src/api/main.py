"""FastAPI entrypoint. Mirrors Forced Action's main.py convention: mounts
every router, no business logic lives here."""

from fastapi import FastAPI

from src.api.akrash_ingest_router import router as akrash_router

app = FastAPI(title="Blackink API")

app.include_router(akrash_router)


@app.get("/healthz")
def healthz():
	return {"status": "ok"}
