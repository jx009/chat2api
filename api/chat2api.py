import asyncio
import os
import types
from typing import Optional

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
    get_expired_token_entries,
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


def _deduplicate_keep_order(items):
    seen = set()
    result = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _persist_error_tokens():
    unique_tokens = _deduplicate_keep_order(globals.error_token_list)
    globals.error_token_list[:] = unique_tokens
    with open(globals.ERROR_TOKENS_FILE, "w", encoding="utf-8") as f:
        for token in unique_tokens:
            f.write(token + "\n")


def _build_error_token_item(token: str):
    token_key = token[:20]
    return {
        "token": token,
        "token_key": token_key,
        "token_name": get_token_name(token_key) or "",
    }


def _ensure_token_present_in_runtime(token: str):
    if token not in globals.token_list:
        globals.token_list.append(token)
        with open(globals.TOKENS_FILE, "a", encoding="utf-8") as f:
            f.write(token + "\n")


def _reconcile_expired_tokens():
    expired_entries = get_expired_token_entries()
    changed = False
    for _, cfg in expired_entries.items():
        token = str(cfg.get("full_token") or "").strip()
        if not token:
            continue
        _ensure_token_present_in_runtime(token)
        if token not in globals.error_token_list:
            globals.error_token_list.append(token)
            changed = True

    if changed:
        _persist_error_tokens()


def _restore_token_from_error_pool_if_not_expired(token_key: str):
    cfg = get_all_token_configs().get(token_key)
    if not cfg:
        return
    token = str(cfg.get("full_token") or "").strip()
    if not token:
        return

    expired_keys = set(get_expired_token_entries().keys())
    if token_key not in expired_keys and token in globals.error_token_list:
        globals.error_token_list[:] = [t for t in globals.error_token_list if t != token]
        _persist_error_tokens()


@app.post(f"/{api_prefix}/tokens/error" if api_prefix else "/tokens/error")
async def error_tokens():
    _reconcile_expired_tokens()
    error_tokens_list = _deduplicate_keep_order(globals.error_token_list)
    data = [_build_error_token_item(token) for token in error_tokens_list]
    return {"status": "success", "error_tokens": error_tokens_list, "data": data}


class ErrorTokenRequest(BaseModel):
    token: str


@app.post(f"/{api_prefix}/tokens/error/add" if api_prefix else "/tokens/error/add")
async def add_error_token(req: ErrorTokenRequest):
    token = req.token.strip()
    if not token or token.startswith("#"):
        raise HTTPException(status_code=400, detail="Invalid token")

    if token not in globals.token_list:
        globals.token_list.append(token)
        with open(globals.TOKENS_FILE, "a", encoding="utf-8") as f:
            f.write(token + "\n")

    if token not in globals.error_token_list:
        globals.error_token_list.append(token)
        _persist_error_tokens()

    tokens_count = len(set(globals.token_list) - set(globals.error_token_list))
    return {
        "status": "success",
        "tokens_count": tokens_count,
        "token": _build_error_token_item(token),
    }


@app.post(f"/{api_prefix}/tokens/error/remove" if api_prefix else "/tokens/error/remove")
async def remove_error_token(req: ErrorTokenRequest):
    token = req.token.strip()
    if not token:
        raise HTTPException(status_code=400, detail="Invalid token")

    if token in globals.error_token_list:
        globals.error_token_list[:] = [t for t in globals.error_token_list if t != token]
        _persist_error_tokens()

    tokens_count = len(set(globals.token_list) - set(globals.error_token_list))
    return {
        "status": "success",
        "tokens_count": tokens_count,
        "token": _build_error_token_item(token),
    }


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
    expires_at: Optional[str] = None


class TokenRenameRequest(BaseModel):
    name: Optional[str] = None
    expires_at: Optional[str] = None


@app.get(f"/{api_prefix}/codex/usage/{{token_prefix}}" if api_prefix else "/codex/usage/{token_prefix}")
async def get_token_codex_usage(token_prefix: str):
    _reconcile_expired_tokens()
    snapshot = get_codex_snapshot(token_prefix)
    if snapshot:
        cfg = get_all_token_configs().get(token_prefix, {})
        snapshot["token_name"] = get_token_name(token_prefix) or ""
        snapshot["expires_at"] = cfg.get("expires_at")
        return {"status": "success", "data": snapshot}

    cfg = get_all_token_configs().get(token_prefix)
    if cfg:
        return {
            "status": "success",
            "data": {
                "token_key": token_prefix,
                "token_name": cfg.get("name") or "",
                "expires_at": cfg.get("expires_at"),
            },
        }
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
    _reconcile_expired_tokens()
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

    try:
        token_key = add_token_config(token, req.name.strip(), req.expires_at)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    _reconcile_expired_tokens()

    tokens_count = len(set(globals.token_list) - set(globals.error_token_list))
    return {
        "status": "success",
        "token_key": token_key,
        "name": req.name.strip(),
        "expires_at": get_all_token_configs().get(token_key, {}).get("expires_at"),
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