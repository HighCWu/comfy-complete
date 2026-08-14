"""Authenticated streaming gateway for a RunPod Pod-hosted ComfyUI process.

ComfyUI listens only on loopback.  RunPod exposes this gateway's port through
its HTTPS proxy; the Cloudflare Worker injects ``X-Comfy-Pod-Token`` after it
has authenticated and resolved an editor session.  The browser never receives
the Pod credential or provider URL.
"""

from __future__ import annotations

import asyncio
import hmac
import os
from collections.abc import AsyncIterator

from aiohttp import ClientSession, ClientTimeout, WSMsgType, web


TOKEN_HEADER = "X-Comfy-Pod-Token"
HEALTH_PATH = "/__comfy/health"
HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)


def configured_token() -> str:
    token = os.environ.get("COMFY_POD_TOKEN", "")
    if not token:
        raise RuntimeError("COMFY_POD_TOKEN is required")
    return token


def token_matches(provided: str | None, expected: str) -> bool:
    return provided is not None and hmac.compare_digest(provided, expected)


def upstream_base_url() -> str:
    host = os.environ.get("COMFY_INTERNAL_HOST", "127.0.0.1")
    port = int(os.environ.get("COMFY_INTERNAL_PORT", "8188"))
    return f"http://{host}:{port}"


def forwarded_headers(request: web.Request) -> dict[str, str]:
    blocked = HOP_BY_HOP_HEADERS | {
        "host",
        "content-length",
        TOKEN_HEADER.lower(),
    }
    return {
        name: value
        for name, value in request.headers.items()
        if name.lower() not in blocked
    }


def response_headers(headers: object) -> dict[str, str]:
    items = getattr(headers, "items")()
    return {
        name: value
        for name, value in items
        if name.lower() not in HOP_BY_HOP_HEADERS | {"content-length"}
    }


async def request_body(request: web.Request) -> AsyncIterator[bytes]:
    async for chunk in request.content.iter_chunked(64 * 1024):
        yield chunk


async def relay_websocket(
    source: web.WebSocketResponse | object,
    destination: web.WebSocketResponse | object,
) -> None:
    async for message in source:
        if message.type == WSMsgType.TEXT:
            await destination.send_str(message.data)
        elif message.type == WSMsgType.BINARY:
            await destination.send_bytes(message.data)
        elif message.type == WSMsgType.PING:
            await destination.ping(message.data)
        elif message.type == WSMsgType.PONG:
            await destination.pong(message.data)
        elif message.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR}:
            break


async def proxy_websocket(request: web.Request) -> web.StreamResponse:
    session: ClientSession = request.app["client_session"]
    upstream_url = upstream_base_url() + request.rel_url.path_qs
    downstream = web.WebSocketResponse(autoping=True, heartbeat=30)
    await downstream.prepare(request)

    try:
        async with session.ws_connect(
            upstream_url,
            headers=forwarded_headers(request),
            autoping=True,
            heartbeat=30,
            max_msg_size=0,
        ) as upstream:
            downstream_to_upstream = asyncio.create_task(
                relay_websocket(downstream, upstream)
            )
            upstream_to_downstream = asyncio.create_task(
                relay_websocket(upstream, downstream)
            )
            done, pending = await asyncio.wait(
                {downstream_to_upstream, upstream_to_downstream},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*done, *pending, return_exceptions=True)
    finally:
        await downstream.close()
    return downstream


async def proxy_http(request: web.Request) -> web.StreamResponse:
    session: ClientSession = request.app["client_session"]
    upstream_url = upstream_base_url() + request.rel_url.path_qs
    body = request_body(request) if request.can_read_body else None
    async with session.request(
        request.method,
        upstream_url,
        headers=forwarded_headers(request),
        data=body,
        allow_redirects=False,
    ) as upstream:
        downstream = web.StreamResponse(
            status=upstream.status,
            reason=upstream.reason,
            headers=response_headers(upstream.headers),
        )
        await downstream.prepare(request)
        async for chunk in upstream.content.iter_chunked(64 * 1024):
            await downstream.write(chunk)
        await downstream.write_eof()
        return downstream


async def route(request: web.Request) -> web.StreamResponse:
    if request.path == HEALTH_PATH:
        return web.json_response({"ok": True})

    expected = request.app["pod_token"]
    if not token_matches(request.headers.get(TOKEN_HEADER), expected):
        return web.json_response({"error": "unauthorized"}, status=401)

    if request.headers.get("Upgrade", "").lower() == "websocket":
        return await proxy_websocket(request)
    return await proxy_http(request)


async def create_client_session(app: web.Application) -> None:
    app["client_session"] = ClientSession(
        timeout=ClientTimeout(total=None, connect=15, sock_connect=15)
    )


async def close_client_session(app: web.Application) -> None:
    await app["client_session"].close()


def create_app(token: str | None = None) -> web.Application:
    app = web.Application(client_max_size=0)
    app["pod_token"] = token or configured_token()
    app.on_startup.append(create_client_session)
    app.on_cleanup.append(close_client_session)
    app.router.add_route("*", "/{path:.*}", route)
    return app


if __name__ == "__main__":
    web.run_app(
        create_app(),
        host="0.0.0.0",
        port=int(os.environ.get("COMFY_POD_PORT", "8189")),
        access_log_format='%a "%r" %s %Tf',
    )
