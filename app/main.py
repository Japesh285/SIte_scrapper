from fastapi import FastAPI
from app.api.routes import router
from app.db.database import init_db

app = FastAPI(title="Adaptive Job Scraper", version="0.1.0")

app.include_router(router)


@app.on_event("startup")
async def startup():
    await init_db()


@app.get("/")
async def root():
    return {"status": "ok", "service": "Adaptive Job Scraper"}
