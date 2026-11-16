import asyncio
import logging
import socket

import httpx
import pytest
from uvicorn import Config, Server
from uvicorn.logging import AccessFormatter

from omakase.web.access_log import protect_access_log


@pytest.mark.parametrize("status", [200, 404, 416])
def test_credential_is_redacted_before_access_formatting(status):
    protect_access_log()
    logger = logging.getLogger("uvicorn.access")
    path = "/plus/api/native-tv-test/v1/direct/test-id/synthetic-pass?synthetic-query"
    record = logger.makeRecord(
        logger.name,
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1", "GET", path, "1.1", status),
        None,
    )
    assert logger.filter(record)
    formatted = AccessFormatter("%(request_line)s %(status_code)s", use_colors=False).format(record)
    assert "/direct/[redacted]" in formatted
    assert str(status) in formatted
    assert "synthetic-" not in formatted


def test_uvicorn_logs_live_access_without_direct_path_credentials(caplog, monkeypatch):
    from fastapi import FastAPI
    from starlette.responses import Response
    from uvicorn.protocols.http.h11_impl import H11Protocol

    from omakase.web import server as production

    # Exercise the production filter installation without touching Plus's database.
    monkeypatch.setattr(production, "_PLUS_MOUNTED", False)
    app = FastAPI(lifespan=production._lifespan)

    @app.get("/plus/api/native-tv-test/v1/direct/{media_id}/{token}")
    async def direct(media_id: str, token: str, status: int):
        return Response(status_code=status)

    @app.get("/ordinary")
    async def ordinary():
        return Response()

    async def check_requests():
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            server = Server(Config(app, http=H11Protocol, log_config=None, lifespan="on"))
            task = asyncio.create_task(server.serve(sockets=[listener]))
            try:
                async with asyncio.timeout(5):
                    while not server.started:
                        if task.done():
                            task.result()
                            pytest.fail("Uvicorn stopped before starting")
                        await asyncio.sleep(0.01)
                    async with httpx.AsyncClient(
                        base_url=f"http://127.0.0.1:{port}", trust_env=False
                    ) as client:
                        for status in (200, 404, 416):
                            result = await client.get(
                                f"/plus/api/native-tv-test/v1/direct/id/synthetic-{status}",
                                params={"status": status, "secret": "synthetic-query"},
                            )
                            assert result.status_code == status
                        assert (await client.get("/ordinary")).status_code == 200
            finally:
                server.should_exit = True
                await asyncio.wait_for(task, timeout=5)

    with caplog.at_level(logging.INFO, logger="uvicorn.access"):
        asyncio.run(check_requests())
    messages = [record.getMessage() for record in caplog.records if record.name == "uvicorn.access"]
    assert len(messages) == 4
    for status in (200, 404, 416):
        assert any(f'/direct/[redacted] HTTP/1.1" {status}' in message for message in messages)
    assert any('/ordinary HTTP/1.1" 200' in message for message in messages)
    assert all("synthetic-" not in message for message in messages)
