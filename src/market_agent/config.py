from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    alpaca_api_key: SecretStr | None = None
    alpaca_secret_key: SecretStr | None = None
    alpaca_data_url: str = "https://data.alpaca.markets"
    alpaca_trading_url: str = "https://paper-api.alpaca.markets"

    featherless_api_key: SecretStr | None = None
    featherless_base_url: str = "https://api.featherless.ai/v1"
    narrative_model: str = "deepseek-ai/DeepSeek-V4-Pro"
    agent_model: str = "deepseek-ai/DeepSeek-V4-Flash"

    alpaca_paper_trade: bool = True
    order_submission_enabled: bool = False
    agent_loop_timeout_seconds: float = 240

    gcp_project_id: str | None = None

    def alpaca_headers(self) -> dict[str, str]:
        if self.alpaca_api_key is None or self.alpaca_secret_key is None:
            raise ValueError("ALPACA_API_KEY and ALPACA_SECRET_KEY are required")
        return {
            "APCA-API-KEY-ID": self.alpaca_api_key.get_secret_value(),
            "APCA-API-SECRET-KEY": self.alpaca_secret_key.get_secret_value(),
        }

    def model_url(self) -> str:
        return f"{self.featherless_base_url.rstrip('/')}/chat/completions"

    def model_headers(self) -> dict[str, str]:
        if self.featherless_api_key is None:
            raise ValueError("FEATHERLESS_API_KEY is required")
        return {
            "authorization": f"Bearer {self.featherless_api_key.get_secret_value()}",
            "content-type": "application/json",
        }
