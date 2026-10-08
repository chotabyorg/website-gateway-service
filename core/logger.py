"""Only bounded application events may contain request diagnostics.

Protection lives at both record creation and every configured output sink. Calling
setup_logging again is the supported reconfiguration; DEBUG retains protections.
"""
import logging
import re
import sys
from uuid import UUID

_BLOCKED = ("httpx", "httpcore", "aiohttp")
_EVENT = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_ORIGINAL_FACTORY = getattr(logging, "_website_original_record_factory", logging.getLogRecordFactory())
logging._website_original_record_factory = _ORIGINAL_FACTORY


def safe_uuid(value: object) -> str:
    if isinstance(value, str) and len(value) == 36:
        try:
            canonical = str(UUID(value))
            if canonical == value:
                return canonical
        except ValueError:
            pass
    return "opaque"


def safe_error_type(exc: BaseException) -> str:
    name = type(exc).__name__
    return name if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name) else "Exception"


def _blocked(name: str) -> bool:
    return any(name == prefix or name.startswith(prefix + ".") for prefix in _BLOCKED)


def _exception_argument(value) -> bool:
    if isinstance(value, BaseException):
        return True
    if isinstance(value, (tuple, list)):
        return any(_exception_argument(item) for item in value)
    if isinstance(value, dict):
        return any(_exception_argument(item) for item in value.values())
    return False


def _sanitize(record: logging.LogRecord) -> None:
    if _blocked(record.name):
        record.msg, record.args = "dependency_diagnostic_suppressed", ()
    elif record.exc_info or record.exc_text or record.stack_info or _exception_argument(record.msg) or _exception_argument(record.args):
        record.msg, record.args = "exception_diagnostic_suppressed", ()
    record.exc_info = record.exc_text = record.stack_info = None


def _record_factory(*args, **kwargs):
    record = _ORIGINAL_FACTORY(*args, **kwargs)
    _sanitize(record)
    return record


class CredentialSafeFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        _sanitize(record)
        if _blocked(record.name):
            return False
        if record.name == "website-gateway" and not getattr(record, "safe_event", False):
            return False
        return True


def setup_logging(level: str = "INFO") -> None:
    logging.setLogRecordFactory(_record_factory)
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger().setLevel(getattr(logging, level.upper(), logging.INFO))
    safe_filter = CredentialSafeFilter()
    all_loggers = [logging.getLogger(), *[item for item in logging.Logger.manager.loggerDict.values() if isinstance(item, logging.Logger)]]
    for item in all_loggers:
        if _blocked(item.name):
            item.handlers.clear()
            item.propagate = True
            item.disabled = True
        for handler in item.handlers:
            if not any(isinstance(f, CredentialSafeFilter) for f in handler.filters):
                handler.addFilter(safe_filter)
    # Parent levels/disabled do not affect descendants; the record/sink policies do.
    for name in _BLOCKED:
        dependency = logging.getLogger(name)
        dependency.handlers.clear()
        dependency.propagate = True
        dependency.disabled = True


def log_event(level: int, event: str, *, exc: BaseException | None = None, status: object = None, elapsed: float | None = None, **ids) -> None:
    if not _EVENT.fullmatch(event):
        raise ValueError("invalid_diagnostic_event")
    fields = [event]
    if exc is not None:
        fields.append("error_type=" + safe_error_type(exc))
    if isinstance(status, int) and 100 <= status <= 599:
        fields.append("status=" + str(status))
    if elapsed is not None and isinstance(elapsed, (float, int)) and 0 <= elapsed <= 3600:
        fields.append(f"elapsed={elapsed:.3f}")
    for key, value in ids.items():
        if key not in ("agent_id", "session_id", "integration_id", "owner_id", "message_id", "response_attempt_id"):
            raise ValueError("invalid_diagnostic_field")
        fields.append(key + "=" + safe_uuid(value))
    logger.log(level, " ".join(fields), extra={"safe_event": True})


logger = logging.getLogger("website-gateway")
# Import-time install protects library clients before main creates them.
setup_logging()
