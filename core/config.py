from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from core.routing import RoutingPolicy


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", hide_input_in_errors=True)

    CONTROL_PLANE_URL: str = "http://localhost:55580"
    INTERNAL_API_TOKEN: str = ""
    INTEGRATIONS_POLL_INTERVAL: int = 60
    AI_AGENT_SERVICE_URL: str = "http://localhost:55570"
    CHAT_S2S_TOKEN: SecretStr = SecretStr("")
    AI_AGENT_REQUEST_TIMEOUT_SECONDS: float = 120.0

    MANAGED_WEBSITE_GUARDED_INTEGRATIONS: str = "{}"
    MANAGED_WEBSITE_COHORTS: str = "{}"
    CONVERSATION_SERVICE_URL: str = ""
    CONVERSATION_S2S_TOKEN: SecretStr = SecretStr("")
    CONVERSATION_REQUEST_TIMEOUT_SECONDS: float = 10.0
    CONVERSATION_RESULT_WAIT_SECONDS: float = 120.0
    CONVERSATION_RESULT_POLL_SECONDS: float = 0.5
    CONVERSATION_RESULT_POLL_INITIAL_SECONDS: float = 0.1

    HOST: str = "0.0.0.0"
    PORT: int = 8080
    ALLOWED_ORIGINS: str = "*"
    LOG_LEVEL: str = "INFO"

    @model_validator(mode="after")
    def check_managed_configuration(self):
        policy = self.routing_policy()
        if policy.guards and (not self.CONVERSATION_SERVICE_URL or not self.CONVERSATION_S2S_TOKEN.get_secret_value()):
            raise ValueError("guard_requires_conversation_configuration")
        if not 0.1 <= self.CONVERSATION_REQUEST_TIMEOUT_SECONDS <= 30:
            raise ValueError("invalid_conversation_request_timeout")
        if not 0 <= self.CONVERSATION_RESULT_WAIT_SECONDS <= 120:
            raise ValueError("invalid_conversation_result_wait")
        if not 0.05 <= self.CONVERSATION_RESULT_POLL_SECONDS <= 5:
            raise ValueError("invalid_conversation_poll_interval")
        if not 0.05 <= self.CONVERSATION_RESULT_POLL_INITIAL_SECONDS <= self.CONVERSATION_RESULT_POLL_SECONDS:
            raise ValueError("invalid_conversation_poll_initial")
        return self

    def routing_policy(self) -> RoutingPolicy:
        return RoutingPolicy.parse(self.MANAGED_WEBSITE_GUARDED_INTEGRATIONS, self.MANAGED_WEBSITE_COHORTS)


def load_settings() -> Settings:
    try:
        return Settings()
    except Exception:
        # No Pydantic input dump or credential-bearing exception chain on startup.
        raise RuntimeError("invalid_gateway_configuration") from None


settings = load_settings()
