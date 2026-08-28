from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    alpaca_api_key: SecretStr | None = None
    alpaca_secret_key: SecretStr | None = None
    alpaca_data_url: str = "https://data.alpaca.markets"
    alpaca_trading_url: str = "https://paper-api.alpaca.markets"

    cloudflare_account_id: str | None = None
    ai_gateway_token: SecretStr | None = None
    ai_gateway_id: str = "default"
    narrative_model: str = "anthropic/claude-sonnet-4-5"
    agent_model: str = "anthropic/claude-sonnet-4-5"

    alpaca_paper_trade: bool = True
    order_submission_enabled: bool = False
    max_agent_tool_calls: int = 6

    gcp_project_id: str | None = None

    def alpaca_headers(self) -> dict[str, str]:
        if self.alpaca_api_key is None or self.alpaca_secret_key is None:
            raise ValueError("ALPACA_API_KEY and ALPACA_SECRET_KEY are required")
        return {
            "APCA-API-KEY-ID": self.alpaca_api_key.get_secret_value(),
            "APCA-API-SECRET-KEY": self.alpaca_secret_key.get_secret_value(),
        }

    def gateway_url(self) -> str:
        if self.cloudflare_account_id is None or self.ai_gateway_token is None:
            raise ValueError("CLOUDFLARE_ACCOUNT_ID and AI_GATEWAY_TOKEN are required")
        return (
            "https://gateway.ai.cloudflare.com/v1/"
            f"{self.cloudflare_account_id}/{self.ai_gateway_id}/compat/chat/completions"
        )

    def gateway_headers(self) -> dict[str, str]:
        if self.ai_gateway_token is None:
            raise ValueError("AI_GATEWAY_TOKEN is required")
        return {
            "cf-aig-authorization": f"Bearer {self.ai_gateway_token.get_secret_value()}",
            "content-type": "application/json",
        }
