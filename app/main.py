from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import engine, get_session, session_factory
from app.repository import (
    authenticate,
    bootstrap,
    read_settings,
    read_snowluma_settings,
    save_settings,
    save_snowluma_settings,
)
from app.security import make_session, session_username


@asynccontextmanager
async def lifespan(_: FastAPI):
    async with session_factory() as session:
        await bootstrap(session)
    yield
    await engine.dispose()


app = FastAPI(title="VireyaBot", version=settings.vireyabot_version, lifespan=lifespan)


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


def current_user(request: Request) -> str:
    username = session_username(request.cookies.get("vireyabot_session"))
    if not username:
        raise HTTPException(status_code=401, detail="Authentication required")
    return username


SNOWLUMA_PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>VireyaBot · SnowLuma</title><style>
body{font:16px system-ui,sans-serif;max-width:760px;margin:2rem auto;padding:0 1rem;color:#202124}
label{display:block;margin:1rem 0;font-weight:600}input,select{display:block;box-sizing:border-box;width:100%;padding:.6rem;margin-top:.35rem;font:inherit}
button{padding:.6rem 1rem;margin:.4rem .4rem .4rem 0;font:inherit}.hint{color:#5f6368;font-size:.9rem;font-weight:400}.error{color:#b00020}.ok{color:#176b35}
#settings{display:none}.status{min-height:1.5rem}.secret{letter-spacing:.15em}.actions{margin-top:1rem}
</style></head><body><h1>SnowLuma / OneBot v11 配置</h1>
<section id="login"><h2>管理员登录</h2><form id="login-form"><label>管理员<input name="username" value="admin" required></label><label>密码<input name="password" type="password" required></label><button>登录</button><p class="error" id="login-error"></p></form></section>
<section id="settings"><p><a href="/">返回通用配置</a>　<button id="logout" type="button">退出</button></p>
<p class="hint">这里只保存连接参数，不会验证连接、发送 QQ 消息或探测外部网络。保存成功只代表参数已写入数据库。</p>
<form id="snowluma-form"><label>连接模式<select name="connection_mode"><option value="reverse_ws">反向 WebSocket：SnowLuma 连接本服务</option><option value="forward_ws">正向 WebSocket：本服务连接 SnowLuma</option><option value="http">HTTP：本服务调用 SnowLuma</option></select></label>
<label>WebSocket 地址<input name="websocket_url" placeholder="ws://127.0.0.1:6700" autocomplete="url"><span class="hint">仅 WebSocket 模式填写；反向模式是本服务接收地址，正向模式是外部 SnowLuma 地址。</span></label>
<label>HTTP 地址<input name="http_url" placeholder="http://127.0.0.1:5700" autocomplete="url"><span class="hint">仅 HTTP 模式填写；这是本服务调用的外部 SnowLuma 地址。</span></label>
<label>Token<input class="secret" name="token" type="password" placeholder="留空保持原 Token"><span class="hint" id="token-state">未配置。Token 仅加密存储，不会回传明文。</span></label>
<div class="actions"><button>保存参数</button><button id="reload" type="button">重新读取</button></div><p class="status" id="save-status"></p></form></section>
<script>
const $=s=>document.querySelector(s); const mask="••••••••";
function showSettings(){$("#login").style.display="none";$("#settings").style.display="block"}
async function load(){const r=await fetch("/api/snowluma");if(r.status===401){return}if(!r.ok){$("#save-status").textContent="读取失败";$("#save-status").className="status error";return}const v=await r.json();showSettings();for(const key of ["connection_mode","websocket_url","http_url"]){$(`[name=${key}]`).value=v[key]||""}$("[name=token]").value="";$("#token-state").textContent=v.token_configured?"已配置。留空保持原 Token；输入新值将替换。":"未配置。Token 仅加密存储，不会回传明文。"}
$("#login-form").onsubmit=async e=>{e.preventDefault();const r=await fetch("/api/login",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(Object.fromEntries(new FormData(e.target)))});if(r.ok){$("#login-error").textContent="";load()}else $("#login-error").textContent="登录失败"};
$("#snowluma-form").onsubmit=async e=>{e.preventDefault();const f=new FormData(e.target);const payload=Object.fromEntries(f);if(!payload.token)delete payload.token;const r=await fetch("/api/snowluma",{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});const body=await r.json();$("#save-status").textContent=r.ok?"参数已保存；未进行连接验证。":(body.detail||"保存失败");$("#save-status").className="status "+(r.ok?"ok":"error");if(r.ok)load()};
$("#reload").onclick=load;$("#logout").onclick=async()=>{await fetch("/api/logout",{method:"POST"});location.reload()};load();
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return SNOWLUMA_PAGE


@app.get("/snowluma", response_class=HTMLResponse)
async def snowluma_page() -> str:
    return SNOWLUMA_PAGE


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
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"status": "saved", "connection": "not_verified"}
