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

    # Bitrix24 REST API (polling mode)
    BITRIX24_WEBHOOK_URL: str = ""
    BITRIX24_USER_ID: SecretStr = SecretStr("")
    BITRIX24_POLL_INTERVAL_SECONDS: int = 60
    BITRIX24_RESPONSIBLE_IDS: str = ""  # comma-separated, e.g. "70,71,72"

    # GLPI defaults for ticket creation
    GLPI_DEFAULT_CATEGORY_ID: int = 1  # Инцидент
    GLPI_DEFAULT_GROUP_ID: int = 1  # IT-поддержка L1
    GLPI_DEFAULT_ENTITY_ID: int = 2  # Департамент IT

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    @property
    def responsible_ids(self) -> list[int]:
        """Parse comma-separated responsible IDs into a list."""
        if not self.BITRIX24_RESPONSIBLE_IDS:
            return []
        return [int(x.strip()) for x in self.BITRIX24_RESPONSIBLE_IDS.split(",") if x.strip()]


settings = Settings()
