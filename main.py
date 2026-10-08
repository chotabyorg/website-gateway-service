import asyncio
import contextlib
import logging

from aiohttp import web

from api.handlers import handle_chat, handle_config, handle_health, handle_root
from api.middleware import cors_middleware, safe_error_middleware
from core.logger import log_event, setup_logging
from core.config import settings
from services.control_plane_client import ControlPlaneClient
from services.core_api import CoreAPIClient
from services.conversation_client import ConversationClient
from services.widget_key_manager import WidgetKeyManager


def create_app(key_manager, core_api, conversation=None) -> web.Application:
    app = web.Application(middlewares=[safe_error_middleware, cors_middleware])
    app["key_manager"] = key_manager
    app["core_api"] = core_api
    app["conversation_client"] = conversation
    app["routing_policy"] = settings.routing_policy()
    app["transport_timeout"] = settings.CONVERSATION_REQUEST_TIMEOUT_SECONDS
    app.router.add_post("/v1/chat", handle_chat)
    app.router.add_get("/v1/config", handle_config)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/", handle_root)
    return app


def create_runner(app: web.Application) -> web.AppRunner:
    # Required for /v1/config?key=...; logger levels alone are not protection.
    return web.AppRunner(app, access_log=None)


async def main() -> None:
    setup_logging(settings.LOG_LEVEL)

    cp_client = ControlPlaneClient(
        base_url=settings.CONTROL_PLANE_URL,
        internal_token=settings.INTERNAL_API_TOKEN,
    )
    core_api = CoreAPIClient(base_url=settings.AI_AGENT_SERVICE_URL)
    key_manager = WidgetKeyManager(cp_client)

    conversation = None
    if settings.routing_policy().guards:
        conversation = ConversationClient(
            settings.CONVERSATION_SERVICE_URL, settings.CONVERSATION_S2S_TOKEN.get_secret_value(),
            request_timeout=settings.CONVERSATION_REQUEST_TIMEOUT_SECONDS,
            result_wait=settings.CONVERSATION_RESULT_WAIT_SECONDS,
            poll_interval=settings.CONVERSATION_RESULT_POLL_SECONDS,
        )
    await key_manager.reconcile()
    if key_manager.active_count() == 0:
        log_event(logging.WARNING, "no_active_widgets")

    app = create_app(key_manager, core_api, conversation)
    runner = create_runner(app)

    async def reconcile_loop() -> None:
        while True:
            await asyncio.sleep(settings.INTEGRATIONS_POLL_INTERVAL)
            await key_manager.reconcile()

    try:
        await runner.setup()
        site = web.TCPSite(runner, host=settings.HOST, port=settings.PORT)
        await site.start()

        log_event(logging.INFO, "gateway_started")

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
        if conversation is not None:
            await conversation.close()
        log_event(logging.INFO, "gateway_stopped")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        log_event(logging.ERROR, "gateway_startup_failed", exc=exc)
        raise SystemExit(1) from None
