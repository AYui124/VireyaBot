from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import engine, get_session, session_factory
from app.repository import authenticate, bootstrap, read_settings, save_settings
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


def current_user(request: Request) -> str:
    username = session_username(request.cookies.get("vireyabot_session"))
    if not username:
        raise HTTPException(status_code=401, detail="Authentication required")
    return username


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return """<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><title>VireyaBot</title><style>body{font:16px system-ui;max-width:720px;margin:3rem auto;padding:0 1rem}label{display:block;margin:.8rem 0}input{width:100%;padding:.5rem}button{padding:.5rem 1rem}#app{display:none}.error{color:#b00}</style></head><body><h1>VireyaBot 配置</h1><section id='login'><form><label>管理员 <input name='username' value='admin' required></label><label>密码 <input name='password' type='password' required></label><button>登录</button><p class='error'></p></form></section><section id='app'><button id='logout'>退出</button><p id='status'></p><form id='settings'><label>应用名称 <input name='app.name'></label><label>日志级别 <input name='logging.level'></label><label>测试秘密 <input name='integration.test_secret' type='password' placeholder='留空保持原值'></label><label><input name='features.proactive_enabled' type='checkbox'> 启用主动会话</label><button>保存</button></form></section><script>const $=s=>document.querySelector(s);async function load(){let r=await fetch('/api/settings');if(!r.ok)return;let v=await r.json();$('#login').style.display='none';$('#app').style.display='block';for(const [k,x] of Object.entries(v)){let e=document.querySelector(`[name="${k}"]`);if(!e)continue;if(e.type==='checkbox')e.checked=x;else e.value=x;} }$('#login form').onsubmit=async e=>{e.preventDefault();let f=new FormData(e.target);let r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(Object.fromEntries(f))});if(r.ok)load();else $('.error').textContent='登录失败';};$('#settings').onsubmit=async e=>{e.preventDefault();let values={};for(let e of e.target.elements)if(e.name)values[e.name]=e.type==='checkbox'?e.checked:e.value;let r=await fetch('/api/settings',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({values})});$('#status').textContent=r.ok?'已保存':'保存失败';if(r.ok)load();};$('#logout').onclick=async()=>{await fetch('/api/logout',{method:'POST'});location.reload()};load();</script></body></html>"""


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
        return {
            "application": "ok",
            "database": "ok",
            "version": settings.vireyabot_version,
            "migration": migration or "pending",
        }
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Database or migration unavailable") from exc

@app.get("/status", response_class=HTMLResponse)
async def status_page() -> str:
    return """<!doctype html><html lang='zh-CN'><meta charset='utf-8'><title>VireyaBot 状态</title><h1>VireyaBot 状态</h1><pre id='state'>加载中…</pre><script>fetch('/api/status').then(async r=>{document.querySelector('#state').textContent=JSON.stringify(await r.json(),null,2)}).catch(e=>document.querySelector('#state').textContent=e)</script></html>"""


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
