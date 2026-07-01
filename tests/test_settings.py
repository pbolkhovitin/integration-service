"""Tests for app.config.settings.Settings — Pydantic BaseSettings."""

import pytest


class TestSettingsDefaults:
    """Default values from the class definition."""

    def test_default_app_name(self):
        from app.config.settings import Settings

        s = Settings()
        assert s.APP_NAME == "Integration Service"

    def test_default_app_host(self):
        from app.config.settings import Settings

        s = Settings()
        assert s.APP_HOST == "0.0.0.0"

    def test_default_app_port(self):
        from app.config.settings import Settings

        s = Settings()
        assert s.APP_PORT == 8000

    def test_default_empty_strings(self):
        from app.config.settings import Settings

        s = Settings()
        assert s.GLPI_URL == ""
        assert s.GLPI_APP_TOKEN == ""
        assert s.GLPI_USER_TOKEN == ""
        assert s.BITRIX24_WEBHOOK_URL == ""
        assert s.BITRIX24_USER_ID == ""


class TestSettingsEnvOverrides:
    """Environment variables override defaults."""

    def test_env_overrides_app_name(self, monkeypatch):
        monkeypatch.setenv("APP_NAME", "Custom Integration Service")
        from app.config.settings import Settings

        assert Settings().APP_NAME == "Custom Integration Service"

    def test_env_overrides_app_port(self, monkeypatch):
        monkeypatch.setenv("APP_PORT", "9090")
        from app.config.settings import Settings

        assert Settings().APP_PORT == 9090

    def test_env_overrides_glpi_url(self, monkeypatch):
        monkeypatch.setenv("GLPI_URL", "http://custom-glpi:8080")
        from app.config.settings import Settings

        assert Settings().GLPI_URL == "http://custom-glpi:8080"

    def test_env_overrides_glpi_tokens(self, monkeypatch):
        monkeypatch.setenv("GLPI_APP_TOKEN", "app-token-12345")
        monkeypatch.setenv("GLPI_USER_TOKEN", "user-token-67890")
        from app.config.settings import Settings

        s = Settings()
        assert s.GLPI_APP_TOKEN == "app-token-12345"
        assert s.GLPI_USER_TOKEN == "user-token-67890"

    def test_env_overrides_bitrix24(self, monkeypatch):
        monkeypatch.setenv("BITRIX24_WEBHOOK_URL", "https://b24.example.com/hook")
        monkeypatch.setenv("BITRIX24_USER_ID", "42")
        from app.config.settings import Settings

        s = Settings()
        assert s.BITRIX24_WEBHOOK_URL == "https://b24.example.com/hook"
        assert s.BITRIX24_USER_ID == "42"

    def test_env_overrides_all_app_settings(self, monkeypatch):
        monkeypatch.setenv("APP_NAME", "Prod Integration")
        monkeypatch.setenv("APP_HOST", "127.0.0.1")
        monkeypatch.setenv("APP_PORT", "443")
        from app.config.settings import Settings

        s = Settings()
        assert s.APP_NAME == "Prod Integration"
        assert s.APP_HOST == "127.0.0.1"
        assert s.APP_PORT == 443


class TestSettingsSingleton:
    """Module-level settings instance."""

    def test_singleton_is_settings_instance(self):
        from app.config.settings import settings
        from app.config.settings import Settings

        assert isinstance(settings, Settings)

    def test_singleton_defaults_match_class_defaults(self):
        from app.config.settings import settings

        assert settings.APP_NAME == "Integration Service"
        assert settings.APP_HOST == "0.0.0.0"
        assert settings.APP_PORT == 8000

    def test_singleton_is_same_reference(self):
        from app.config.settings import settings
        import app.config.settings as mod

        assert settings is mod.settings


class TestSettingsModelConfig:
    """model_config uses .env file."""

    def test_model_config_has_env_file(self):
        from app.config.settings import Settings

        assert Settings.model_config.get("env_file") == ".env"

    def test_model_config_has_env_file_encoding(self):
        from app.config.settings import Settings

        assert Settings.model_config.get("env_file_encoding") == "utf-8"

    def test_settings_subclasses_basesettings(self):
        from app.config.settings import Settings
        from pydantic_settings import BaseSettings

        assert issubclass(Settings, BaseSettings)
