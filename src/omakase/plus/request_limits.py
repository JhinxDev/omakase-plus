"""Bound request streams before authentication dependencies parse forms."""

from __future__ import annotations

from fastapi import HTTPException, Request
from fastapi.routing import APIRoute
from python_multipart.exceptions import MultipartParseError
from starlette.requests import ClientDisconnect


def bounded_form_route(max_bytes: int, too_large: str, *, guard=None):
    class BoundedFormRoute(APIRoute):
        def get_route_handler(self):
            original = super().get_route_handler()

            async def bounded(request: Request):
                if guard:
                    guard()
                if request.method not in {"POST", "PUT", "PATCH"}:
                    return await original(request)
                try:
                    declared = int(request.headers.get("content-length", "0"))
                    if declared < 0:
                        raise ValueError
                except ValueError:
                    raise HTTPException(400, "Invalid form length") from None
                if declared > max_bytes:
                    raise HTTPException(413, too_large)
                chunks, count = [], 0
                try:
                    async for chunk in request.stream():
                        count += len(chunk)
                        if count > max_bytes:
                            raise HTTPException(413, too_large)
                        chunks.append(chunk)
                except ClientDisconnect:
                    raise HTTPException(400, "Incomplete form") from None
                body = b"".join(chunks)
                consumed = False

                async def receive():
                    nonlocal consumed
                    if consumed:
                        return {"type": "http.disconnect"}
                    consumed = True
                    return {"type": "http.request", "body": body, "more_body": False}

                limited = Request(request.scope, receive=receive)
                # Cache the bounded body before shared CSRF parsing consumes it.
                await limited.body()
                try:
                    return await original(limited)
                except MultipartParseError:
                    raise HTTPException(400, "Invalid upload form") from None
                finally:
                    await limited.close()

            return bounded

    return BoundedFormRoute
