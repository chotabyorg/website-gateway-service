from pydantic import SecretStr
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    CONTROL_PLANE_URL: str = "http://localhost:55580"
    INTERNAL_API_TOKEN: str = ""
    INTEGRATIONS_POLL_INTERVAL: int = 60

    AI_AGENT_SERVICE_URL: str = "http://localhost:55570"
    CHAT_S2S_TOKEN: SecretStr = SecretStr("")
    AI_AGENT_REQUEST_TIMEOUT_SECONDS: float = 120.0

    HOST: str = "0.0.0.0"
    PORT: int = 8080

    ALLOWED_ORIGINS: str = "*"
    LOG_LEVEL: str = "INFO"

    class Config:
        env_file = ".env"


settings = Settings()
