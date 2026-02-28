import asyncio
import os
import types

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import Request, HTTPException, Form, Security
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel
from starlette.background import BackgroundTask

import utils.globals as globals
from app import app, templates, security_scheme
from chatgpt.ChatService import ChatService
from chatgpt.authorization import refresh_all_tokens
from chatgpt.codexUsage import (
    get_codex_snapshot,
    get_all_codex_snapshots_with_names,
    add_token_config,
    update_token_config,
    delete_token_config,
    get_all_token_configs,
    get_token_name,
)
from utils.Logger import logger
from utils.configs import api_prefix, scheduled_refresh
from utils.retry import async_retry

scheduler = AsyncIOScheduler()


@app.on_event("startup")
async def app_start():
    if scheduled_refresh:
        scheduler.add_job(id='refresh', func=refresh_all_tokens, trigger='cron', hour=3, minute=0, day='*/2',
                          kwargs={'force_refresh': True})
        scheduler.start()
        asyncio.get_event_loop().call_later(0, lambda: asyncio.create_task(refresh_all_tokens(force_refresh=False)))


async def to_send_conversation(request_data, req_token):
    chat_service = ChatService(req_token)
    try:
        await chat_service.set_dynamic_data(request_data)
        await chat_service.get_chat_requirements()
        return chat_service
    except HTTPException as e:
        await chat_service.close_client()
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    except Exception as e:
        await chat_service.close_client()
        logger.error(f"Server error, {str(e)}")
        raise HTTPException(status_code=500, detail="Server error")


async def process(request_data, req_token):
    chat_service = await to_send_conversation(request_data, req_token)
    await chat_service.prepare_send_conversation()
    res = await chat_service.send_conversation()
    return chat_service, res


@app.post(f"/{api_prefix}/v1/chat/completions" if api_prefix else "/v1/chat/completions")
async def send_conversation(request: Request, credentials: HTTPAuthorizationCredentials = Security(security_scheme)):
    req_token = credentials.credentials
    try:
        request_data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail={"error": "Invalid JSON body"})
    chat_service, res = await async_retry(process, request_data, req_token)
    try:
        if isinstance(res, types.AsyncGeneratorType):
            background = BackgroundTask(chat_service.close_client)
            return StreamingResponse(res, media_type="text/event-stream", background=background)
        else:
            background = BackgroundTask(chat_service.close_client)
            return JSONResponse(res, media_type="application/json", background=background)
    except HTTPException as e:
        await chat_service.close_client()
        if e.status_code == 500:
            logger.error(f"Server error, {str(e)}")
            raise HTTPException(status_code=500, detail="Server error")
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    except Exception as e:
        await chat_service.close_client()
        logger.error(f"Server error, {str(e)}")
        raise HTTPException(status_code=500, detail="Server error")


@app.get(f"/{api_prefix}/tokens" if api_prefix else "/tokens", response_class=HTMLResponse)
async def upload_html(request: Request):
    tokens_count = len(set(globals.token_list) - set(globals.error_token_list))
    return templates.TemplateResponse("tokens.html",
                                      {"request": request, "api_prefix": api_prefix, "tokens_count": tokens_count})


@app.post(f"/{api_prefix}/tokens/upload" if api_prefix else "/tokens/upload")
async def upload_post(text: str = Form(...)):
    lines = text.split("\n")
    for line in lines:
        if line.strip() and not line.startswith("#"):
            globals.token_list.append(line.strip())
            with open(globals.TOKENS_FILE, "a", encoding="utf-8") as f:
                f.write(line.strip() + "\n")
    logger.info(f"Token count: {len(globals.token_list)}, Error token count: {len(globals.error_token_list)}")
    tokens_count = len(set(globals.token_list) - set(globals.error_token_list))
    return {"status": "success", "tokens_count": tokens_count}


@app.post(f"/{api_prefix}/tokens/clear" if api_prefix else "/tokens/clear")
async def clear_tokens():
    globals.token_list.clear()
    globals.error_token_list.clear()
    with open(globals.TOKENS_FILE, "w", encoding="utf-8") as f:
        pass
    logger.info(f"Token count: {len(globals.token_list)}, Error token count: {len(globals.error_token_list)}")
    tokens_count = len(set(globals.token_list) - set(globals.error_token_list))
    return {"status": "success", "tokens_count": tokens_count}


@app.post(f"/{api_prefix}/tokens/error" if api_prefix else "/tokens/error")
async def error_tokens():
    error_tokens_list = list(set(globals.error_token_list))
    return {"status": "success", "error_tokens": error_tokens_list}


@app.get(f"/{api_prefix}/tokens/add/{{token}}" if api_prefix else "/tokens/add/{token}")
async def add_token(token: str):
    if token.strip() and not token.startswith("#"):
        globals.token_list.append(token.strip())
        with open(globals.TOKENS_FILE, "a", encoding="utf-8") as f:
            f.write(token.strip() + "\n")
    logger.info(f"Token count: {len(globals.token_list)}, Error token count: {len(globals.error_token_list)}")
    tokens_count = len(set(globals.token_list) - set(globals.error_token_list))
    return {"status": "success", "tokens_count": tokens_count}


@app.post(f"/{api_prefix}/seed_tokens/clear" if api_prefix else "/seed_tokens/clear")
async def clear_seed_tokens():
    globals.seed_map.clear()
    globals.conversation_map.clear()
    with open(globals.SEED_MAP_FILE, "w", encoding="utf-8") as f:
        f.write("{}")
    with open(globals.CONVERSATION_MAP_FILE, "w", encoding="utf-8") as f:
        f.write("{}")
    logger.info(f"Seed token count: {len(globals.seed_map)}")
    return {"status": "success", "seed_tokens_count": len(globals.seed_map)}


class TokenConfigRequest(BaseModel):
    token: str
    name: str = ""


class TokenRenameRequest(BaseModel):
    name: str


@app.get(f"/{api_prefix}/codex/usage/{{token_prefix}}" if api_prefix else "/codex/usage/{token_prefix}")
async def get_token_codex_usage(token_prefix: str):
    snapshot = get_codex_snapshot(token_prefix)
    if snapshot:
        snapshot["token_name"] = get_token_name(token_prefix) or ""
        return {"status": "success", "data": snapshot}
    return {"status": "not_found", "data": None}


@app.get(f"/{api_prefix}/codex/runtime_tokens/stats" if api_prefix else "/codex/runtime_tokens/stats")
async def get_runtime_tokens_stats():
    tokens_count = len(set(globals.token_list) - set(globals.error_token_list))
    return {
        "status": "success",
        "tokens_count": tokens_count,
        "token_list_count": len(globals.token_list),
        "error_token_count": len(globals.error_token_list),
    }


@app.get(f"/{api_prefix}/codex/usage" if api_prefix else "/codex/usage")
async def get_all_codex_usage():
    return {"status": "success", "data": get_all_codex_snapshots_with_names()}



@app.post(f"/{api_prefix}/codex/tokens" if api_prefix else "/codex/tokens")
async def create_token_config(req: TokenConfigRequest):
    token = req.token.strip()
    if not token or token.startswith("#"):
        raise HTTPException(status_code=400, detail="Invalid token")

    if token not in globals.token_list:
        globals.token_list.append(token)
        with open(globals.TOKENS_FILE, "a", encoding="utf-8") as f:
            f.write(token + "\n")

    token_key = add_token_config(token, req.name.strip())
    tokens_count = len(set(globals.token_list) - set(globals.error_token_list))
    return {
        "status": "success",
        "token_key": token_key,
        "name": req.name.strip(),
        "tokens_count": tokens_count,
    }


@app.get(f"/{api_prefix}/codex/tokens" if api_prefix else "/codex/tokens")
async def list_token_configs():
    return {"status": "success", "data": get_all_token_configs()}


@app.put(f"/{api_prefix}/codex/tokens/{{token_key}}" if api_prefix else "/codex/tokens/{token_key}")
async def rename_token_config(token_key: str, req: TokenRenameRequest):
    ok = update_token_config(token_key, req.name)
    if ok:
        return {"status": "success"}
    return {"status": "not_found", "message": "Token not found"}


@app.delete(f"/{api_prefix}/codex/tokens/{{token_key}}" if api_prefix else "/codex/tokens/{token_key}")
async def remove_token_config(token_key: str):
    ok = delete_token_config(token_key)
    if ok:
        return {"status": "success"}
    return {"status": "not_found", "message": "Token not found"}


@app.get(f"/{api_prefix}/codex/dashboard" if api_prefix else "/codex/dashboard", response_class=HTMLResponse)
async def codex_dashboard():
    html_path = os.path.join(os.path.dirname(__file__), "..", "static", "codex_dashboard.html")
    with open(html_path, "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())