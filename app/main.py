from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import engine, get_session, session_factory
from app.repository import (
    authenticate,
    bootstrap,
    create_provider_config,
    list_provider_configs,
    read_settings,
    read_snowluma_settings,
    save_settings,
    save_snowluma_settings,
    update_provider_config,
)
from app.security import make_session, session_username


@asynccontextmanager
async def lifespan(_: FastAPI):
    async with session_factory() as session:
        await bootstrap(session)
    yield
    await engine.dispose()


app = FastAPI(title="VireyaBot", version=settings.vireyabot_version, lifespan=lifespan)

@app.exception_handler(RequestValidationError)
async def validation_error_response(_: Request, exc: RequestValidationError) -> JSONResponse:
    detail = [{"loc": list(error.get("loc", ())), "msg": error.get("msg", "Invalid request"), "type": error.get("type", "value_error")} for error in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": detail})


class LoginRequest(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)


class SettingsRequest(BaseModel):
    values: dict[str, object]


class SnowLumaRequest(BaseModel):
    connection_mode: str
    websocket_url: str = ""
    http_url: str = ""
    token: str | None = None


class ProviderCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    protocol_type: Literal["openai_responses", "openai_chat_completions", "anthropic_messages"] = "openai_responses"
    base_url: str = Field(min_length=1, max_length=500)
    timeout_seconds: float = Field(gt=0, le=600)
    organization: str = Field(default="", max_length=160)
    enabled: bool = True
    api_key: str | None = None
    responses_model: str = Field(min_length=1, max_length=160)
    embedding_model: str = ""
    embedding_dimensions: int | None = Field(default=None, gt=0)
    native_search_enabled: bool = False
    default_stream: bool = False
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_output_tokens: int = Field(default=4096, ge=1, le=32768)
    extra_headers: dict[str, StrictStr] = Field(default_factory=dict, max_length=32)


class ProviderUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    protocol_type: Literal["openai_responses", "openai_chat_completions", "anthropic_messages"] | None = None
    base_url: str | None = Field(default=None, min_length=1, max_length=500)
    timeout_seconds: float | None = Field(default=None, gt=0, le=600)
    organization: str | None = Field(default=None, max_length=160)
    enabled: bool | None = None
    api_key: str | None = None
    responses_model: str | None = Field(default=None, min_length=1, max_length=160)
    embedding_model: str | None = Field(default=None, max_length=160)
    embedding_dimensions: int | None = Field(default=None, gt=0)
    native_search_enabled: bool | None = None
    default_stream: bool | None = None
    max_output_tokens: int | None = Field(default=None, ge=1, le=32768)
    extra_headers: dict[str, StrictStr] | None = Field(default=None, max_length=32)


def current_user(request: Request) -> str:
    username = session_username(request.cookies.get("vireyabot_session"))
    if not username:
        raise HTTPException(status_code=401, detail="Authentication required")
    return username


FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"


def frontend_file(path: str = "index.html") -> FileResponse | HTMLResponse:
    file = FRONTEND_DIST / path
    if file.is_file():
        return FileResponse(file)
    return HTMLResponse("Frontend assets are not built.", status_code=503)


if (FRONTEND_DIST / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")


@app.get("/", response_class=HTMLResponse, response_model=None)
async def index() -> FileResponse | HTMLResponse:
    return frontend_file()


@app.get("/snowluma", response_class=HTMLResponse, response_model=None)
async def snowluma_page() -> FileResponse | HTMLResponse:
    return frontend_file()


@app.get("/settings", response_class=HTMLResponse, response_model=None)
async def settings_page() -> FileResponse | HTMLResponse:
    return frontend_file()


@app.get("/providers", response_class=HTMLResponse, response_model=None)
async def providers_page() -> FileResponse | HTMLResponse:
    return frontend_file()


@app.get("/health/live")
async def liveness() -> dict[str, str]:
    return {"status": "ok", "version": settings.vireyabot_version}


@app.get("/health/ready")
async def readiness(session: AsyncSession = Depends(get_session)) -> dict[str, str]:
    try:
        await session.execute(text("SELECT 1"))
        return {"status": "ready", "database": "ok"}
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Database unavailable") from exc


@app.get("/api/status")
async def status(_: str = Depends(current_user), session: AsyncSession = Depends(get_session)) -> dict[str, str]:
    try:
        await session.execute(text("SELECT 1"))
        migration = await session.scalar(text("SELECT version_num FROM alembic_version LIMIT 1"))
        return {"application": "ok", "database": "ok", "version": settings.vireyabot_version, "migration": migration or "pending"}
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Database or migration unavailable") from exc


@app.post("/api/login")
async def login(payload: LoginRequest, response: Response, session: AsyncSession = Depends(get_session)) -> dict[str, str]:
    if not await authenticate(session, payload.username, payload.password):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    response.set_cookie("vireyabot_session", make_session(payload.username), httponly=True, samesite="lax", max_age=86_400)
    return {"status": "ok"}


@app.post("/api/logout")
async def logout(response: Response) -> dict[str, str]:
    response.delete_cookie("vireyabot_session")
    return {"status": "ok"}


@app.get("/api/me")
async def me(username: str = Depends(current_user)) -> dict[str, str]:
    return {"username": username}


@app.get("/api/providers")
async def get_providers(_: str = Depends(current_user), session: AsyncSession = Depends(get_session)) -> list[dict[str, object]]:
    return await list_provider_configs(session)


@app.post("/api/providers")
async def post_provider(payload: ProviderCreateRequest, username: str = Depends(current_user), session: AsyncSession = Depends(get_session)) -> dict[str, object]:
    try:
        return await create_provider_config(session, payload.model_dump(), username)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.put("/api/providers/{provider_id}")
async def put_provider(provider_id: str, payload: ProviderUpdateRequest, username: str = Depends(current_user), session: AsyncSession = Depends(get_session)) -> dict[str, object]:
    try:
        return await update_provider_config(session, provider_id, payload.model_dump(exclude_unset=True), username)
    except (TypeError, ValueError) as exc:
        status_code = 404 if str(exc) == "Unknown providerId" else 422
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


@app.get("/api/settings")
async def get_settings(_: str = Depends(current_user), session: AsyncSession = Depends(get_session)) -> dict[str, object]:
    return await read_settings(session)


@app.put("/api/settings")
async def put_settings(payload: SettingsRequest, username: str = Depends(current_user), session: AsyncSession = Depends(get_session)) -> dict[str, str]:
    try:
        await save_settings(session, payload.values, username)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"status": "ok"}


@app.get("/api/snowluma")
async def get_snowluma(_: str = Depends(current_user), session: AsyncSession = Depends(get_session)) -> dict[str, object]:
    return await read_snowluma_settings(session)


@app.put("/api/snowluma")
async def put_snowluma(payload: SnowLumaRequest, username: str = Depends(current_user), session: AsyncSession = Depends(get_session)) -> dict[str, str]:
    try:
        await save_snowluma_settings(session, payload.model_dump(exclude_none=True), username)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"status": "saved", "connection": "not_verified"}


@app.get("/{path:path}", response_class=HTMLResponse, response_model=None)
async def frontend_route(path: str) -> FileResponse | HTMLResponse:
    if path.startswith(("api/", "health/")):
        raise HTTPException(status_code=404, detail="Not found")
    return frontend_file()
