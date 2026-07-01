from pydantic import SecretStr
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # App
    APP_NAME: str = "Integration Service"
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000

    # GLPI (App-Token auth — Phase 1 MVP, synchronous)
    GLPI_URL: str = ""
    GLPI_APP_TOKEN: SecretStr = SecretStr("")
    GLPI_USER_TOKEN: SecretStr = SecretStr("")

    # Bitrix24
    BITRIX24_WEBHOOK_URL: str = ""
    BITRIX24_USER_ID: SecretStr = SecretStr("")

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
