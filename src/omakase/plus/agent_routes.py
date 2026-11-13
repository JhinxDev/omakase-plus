"""Private, authenticated setup help; never starts an agent server from the web."""

from fastapi import APIRouter, Depends, HTTPException, Request

from omakase.plus.connections import private_connections_enabled
from omakase.plus.middleware import require_user


def create_agent_router(templates, context_factory):
    router = APIRouter()

    @router.get("/settings/agents")
    async def agents(request: Request, user=Depends(require_user)):
        if not private_connections_enabled():
            raise HTTPException(404, "Not found")
        return templates.TemplateResponse(
            request,
            "agents.html",
            {**context_factory(request, user, active_page="settings"), "agent_user_id": user.id},
            headers={"Cache-Control": "private, no-store"},
        )

    return router
