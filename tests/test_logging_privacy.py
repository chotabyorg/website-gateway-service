import io
import json
import logging
import subprocess
import sys
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import quote

import httpx
import pytest
from aiohttp import web

from core.logger import log_event, safe_uuid, setup_logging
from services.control_plane_client import ControlPlaneClient, IntegrationConfig
from services.core_api import CoreAPIClient
from services.widget_key_manager import WidgetInfo, WidgetKeyManager

SENTINELS = ['sentinel-widget-query-SECRET', 'sentinel-header-token-SECRET',
             'sentinel-config-SECRET', 'sentinel-response-echo-SECRET',
             'sentinel-proof-SECRET', 'sentinel%2Fencoded?key=SECRET', 'injected\nFAKE_LOG secret=SECRET']


@pytest.fixture
def captured_sinks():
    streams = [io.StringIO(), io.StringIO()]
    handlers = [logging.StreamHandler(stream) for stream in streams]
    for handler in handlers:
        handler.setFormatter(logging.Formatter('%(name)s %(levelname)s %(message)s'))
        logging.getLogger().addHandler(handler)
    child = logging.getLogger('httpcore.http11.arbitrary_descendant')
    # Even a later independently handled descendant cannot print original data:
    # record creation sanitizes it; setup additionally closes every known sink.
    child.handlers.append(handlers[1])
    child.disabled = False
    child.setLevel(logging.DEBUG)
    setup_logging('DEBUG')
    yield streams
    for handler in handlers:
        logging.getLogger().removeHandler(handler)
        child.removeHandler(handler)
        handler.close()
    setup_logging('INFO')


def assert_private(streams):
    output = '\n'.join(stream.getvalue() for stream in streams)
    for secret in SENTINELS:
        assert secret not in output
        assert quote(secret, safe='') not in output
        assert json.dumps(secret)[1:-1] not in output
    return output


@pytest.mark.parametrize('level', ['INFO', 'DEBUG'])
@pytest.mark.asyncio
async def test_httpx_success404_status_and_transport_use_only_safe_diagnostics(captured_sinks, level):
    setup_logging(level)
    client = ControlPlaneClient('http://control-plane', SENTINELS[1])
    await client.client.aclose()
    states = [200, 404, 503]
    def respond(request):
        state = states.pop(0)
        if state == 200:
            return httpx.Response(200, json={'owner_id': 'opaque', 'integration_id': 'opaque',
                'agent_id': 'opaque', 'config': {}, 'conversation_required': False,
                'binding_generation': 1})
        return httpx.Response(state, json={'detail': SENTINELS[3]})
    client.client = httpx.AsyncClient(base_url='http://control-plane', transport=httpx.MockTransport(respond))
    assert await client.validate_widget_key(SENTINELS[0]) is not None
    assert await client.validate_widget_key(SENTINELS[0]) is None
    with pytest.raises(httpx.HTTPError):
        await client.validate_widget_key(SENTINELS[0])
    await client.client.aclose()
    async def broken(request):
        raise httpx.ReadTimeout(SENTINELS[0] + SENTINELS[1], request=request)
    client.client = httpx.AsyncClient(base_url='http://control-plane', transport=httpx.MockTransport(broken))
    with pytest.raises(httpx.HTTPError):
        await client.validate_widget_key(SENTINELS[0])
    await client.close()
    output = assert_private(captured_sinks)
    assert 'widget_validation_response status=200' in output
    assert 'widget_validation_response status=404' in output
    assert 'widget_validation_response status=503' in output
    assert 'widget_validation_unavailable error_type=ReadTimeout' in output


@pytest.mark.asyncio
async def test_legacy_session_errors_reconcile_and_config_repr_are_safe(captured_sinks):
    client = CoreAPIClient('http://ai')
    for secret in SENTINELS:
        client.client.post = AsyncMock(side_effect=httpx.ReadTimeout(secret))
        assert 'слишком' in await client.send_message(secret, secret, secret)
        client.client.post = AsyncMock(side_effect=httpx.HTTPError(secret))
        assert 'недоступен' in await client.send_message(secret, secret, secret)
    client.client.post = AsyncMock(return_value=httpx.Response(200, request=httpx.Request('POST', 'http://ai'), json={'answer': 'safe-reply'}))
    assert await client.send_message(SENTINELS[0], SENTINELS[-1], 'safe') == 'safe-reply'
    await client.close()
    cp = AsyncMock()
    cp.fetch_integrations.side_effect = ValueError(SENTINELS[2])
    manager = WidgetKeyManager(cp)
    await manager.reconcile()
    cp.fetch_integrations.side_effect = None
    cp.fetch_integrations.return_value = [IntegrationConfig('opaque', 'opaque', 'opaque', SENTINELS[2])]
    await manager.reconcile()
    output = assert_private(captured_sinks)
    assert 'legacy_ai_timeout' in output
    assert 'legacy_ai_unavailable' in output
    assert 'legacy_ai_response' in output
    assert 'widget_reconcile_unavailable error_type=AttributeError' in output
    assert 'session_id=opaque' in output


def test_descendants_raw_tracebacks_and_reconfiguration_cannot_leak(captured_sinks):
    for level in ['INFO', 'DEBUG']:
        setup_logging(level)
        logging.getLogger().setLevel(logging.DEBUG)
        for name in ['httpx.request.child', 'httpcore.http11.headers', 'aiohttp.access', 'aiohttp.server']:
            dep = logging.getLogger(name)
            dep.disabled = False
            dep.setLevel(logging.DEBUG)
            for severity in [logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR]:
                dep.log(severity, 'url=%s header=%s body=%s proof=%s', *SENTINELS[:4])
        try:
            try:
                raise ValueError(SENTINELS[0])
            except ValueError as exc:
                raise RuntimeError(SENTINELS[2]) from exc
        except Exception:
            logging.getLogger('arbitrary-application').error(SENTINELS[3], exc_info=True, stack_info=True)
            logging.getLogger('arbitrary-application').error(ValueError(SENTINELS[4]))
        record = logging.LogRecord('arbitrary-application', logging.ERROR, '', 0, SENTINELS[0], (), None)
        record.exc_text = SENTINELS[1]
        record.stack_info = SENTINELS[2]
        for handler in logging.getLogger().handlers:
            handler.handle(record)
        log_event(logging.WARNING, 'bounded_diagnostic', status=503, session_id=SENTINELS[-1])
    assert 'bounded_diagnostic status=503 session_id=opaque' in assert_private(captured_sinks)


@pytest.mark.asyncio
async def test_actual_runner_access_config_and_unhandled_errors_are_safe(captured_sinks):
    import main
    key = MagicMock()
    key.validate_key.return_value = WidgetInfo('opaque', 'opaque', 'opaque', SENTINELS[0], {})
    key.validate_key_for_chat = AsyncMock(side_effect=ValueError(SENTINELS[2]))
    app = main.create_app(key, AsyncMock())
    runner = main.create_runner(app)
    assert runner._kwargs['access_log'] is None
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}') as client:
            response = await client.get('/v1/config', params={'key': SENTINELS[0]})
            assert response.status_code == 200
            key.validate_key.return_value = None
            response = await client.get('/v1/config', params={'key': SENTINELS[0]})
            assert response.status_code == 404
            response = await client.post('/v1/chat', headers={'X-Widget-Key': SENTINELS[0]},
                                        json={'session_id': SENTINELS[-1], 'message': SENTINELS[3]})
            assert response.status_code == 500
            assert response.json() == {'error': 'Service unavailable'}
            assert SENTINELS[2] not in response.text
    finally:
        await runner.cleanup()
    output = assert_private(captured_sinks)
    assert 'gateway_request_failed error_type=ValueError' in output
    assert 'aiohttp.access' not in output


def test_startup_malformed_configuration_hides_input(tmp_path):
    import os
    env = dict(os.environ, MANAGED_WEBSITE_COHORTS=SENTINELS[2])
    result = subprocess.run([sys.executable, 'main.py'], env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert SENTINELS[2] not in result.stdout + result.stderr
    assert 'invalid_gateway_configuration' in result.stdout + result.stderr


def test_diagnostic_uuid_does_not_change_opaque_legacy_parser():
    assert safe_uuid('opaque') == 'opaque'
    assert safe_uuid(SENTINELS[-1]) == 'opaque'
    value = '11111111-1111-4111-8111-111111111111'
    assert safe_uuid(value) == value
