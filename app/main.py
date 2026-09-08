from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import get_settings
from app.queue import close_arq_pool, get_arq_pool
from app.routers.documents import router as documents_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.arq_pool = await get_arq_pool()
    try:
        yield
    finally:
        await close_arq_pool()


def create_app() -> FastAPI:
    settings = get_settings()
    application = FastAPI(title=settings.app_name, lifespan=lifespan)
    application.include_router(documents_router)

    @application.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return application


app = create_app()
