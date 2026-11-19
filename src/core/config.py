from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env", env_file_encoding="utf-8", extra="forbid",
    )

    api_port: int = Field(8014, ge=1, le=65535)
    ui_port: int = Field(8504, ge=1, le=65535)
    database_path: Path = ROOT / "data/case04_support_rag.db"
    knowledge_base_path: Path = ROOT / "data/knowledge_base"
    min_retrieval_score: float = Field(0.45, gt=0, le=1)
    max_cycle_cost_rub: Decimal = Field(Decimal("1.00"), ge=0)
    max_session_cost_rub: Decimal = Field(Decimal("5.00"), ge=0)
    bitrix_mode: Literal["mock", "live"] = "mock"
    bitrix_webhook_url: SecretStr | None = None
    bitrix_app_token: SecretStr | None = None
    bitrix_client_id: SecretStr | None = None
    bitrix_bot_id: int | None = Field(None, gt=0)
    baseline_manual_minutes: Decimal | None = Field(None, gt=0)
    support_variant: Literal["variant_a", "variant_b"] = "variant_a"
    bitrix_operator_queue_id: int = Field(1, gt=0)
    llm_mode: Literal["mock", "live"] = "mock"
    base_url: str = "http://127.0.0.1:11434/v1"
    api_key: SecretStr = SecretStr("ollama")
    fast_llm_model: str = "qwen2.5:7b"
    smart_llm_model: str = "qwen2.5:32b"
    embedding_model: str = "bge-m3"
    embedding_rub_per_million_input_tokens: Decimal | None = Field(None, ge=0)
    llm_rub_per_million_input_tokens: Decimal | None = Field(None, ge=0)
    llm_rub_per_million_output_tokens: Decimal | None = Field(None, ge=0)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings, env_settings, dotenv_settings,
            YamlConfigSettingsSource(settings_cls, yaml_file=ROOT / "config/settings.yaml"),
            file_secret_settings,
        )

