import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from app.api.routes import router
from app.database import AsyncSessionLocal, Base, engine
from app.models import AppSettings
from app.scheduler.engine import start_scheduler, stop_scheduler

logger = logging.getLogger(__name__)


async def _ensure_app_settings() -> None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(AppSettings).where(AppSettings.id == 1))
        if result.scalar_one_or_none() is None:
            session.add(AppSettings(id=1))
            await session.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    Path("data").mkdir(exist_ok=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables ready")
    await _ensure_app_settings()
    await start_scheduler()
    logger.info("Scheduler started")
    yield
    await stop_scheduler()
    logger.info("Scheduler stopped")


app = FastAPI(title="Ticket Tracker", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)
