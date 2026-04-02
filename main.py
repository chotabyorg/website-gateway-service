import asyncio
import contextlib

from aiohttp import web

from api.handlers import handle_chat, handle_health, handle_root
from api.middleware import cors_middleware
from core.config import settings
from core.logger import logger, setup_logging
from services.control_plane_client import ControlPlaneClient
from services.core_api import CoreAPIClient
from services.widget_key_manager import WidgetKeyManager


async def main() -> None:
    setup_logging(settings.LOG_LEVEL)

    cp_client = ControlPlaneClient(
        base_url=settings.CONTROL_PLANE_URL,
        internal_token=settings.INTERNAL_API_TOKEN,
    )
    core_api = CoreAPIClient(base_url=settings.AI_AGENT_SERVICE_URL)
    key_manager = WidgetKeyManager(cp_client)

    await key_manager.reconcile()
    if key_manager.active_count() == 0:
        logger.warning("No active WEBSITE_WIDGET integrations found on startup")

    app = web.Application(middlewares=[cors_middleware])
    app["key_manager"] = key_manager
    app["core_api"] = core_api

    app.router.add_post("/v1/chat", handle_chat)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/", handle_root)

    runner = web.AppRunner(app)

    async def reconcile_loop() -> None:
        while True:
            await asyncio.sleep(settings.INTEGRATIONS_POLL_INTERVAL)
            await key_manager.reconcile()

    try:
        await runner.setup()
        site = web.TCPSite(runner, host=settings.HOST, port=settings.PORT)
        await site.start()

        logger.info("website-gateway-service started on %s:%s", settings.HOST, settings.PORT)

        poll_task = asyncio.create_task(reconcile_loop())
        try:
            await asyncio.Event().wait()
        finally:
            poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await poll_task
    finally:
        await runner.cleanup()
        await core_api.close()
        await cp_client.close()
        logger.info("Service shutdown complete")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
