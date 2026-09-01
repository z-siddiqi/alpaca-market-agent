import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    alpaca_api_key: str
    alpaca_secret_key: str
    gcp_project_id: str
    alpaca_trading_url: str = "https://paper-api.alpaca.markets"
    alpaca_data_url: str = "https://data.alpaca.markets"
    poll_seconds: float = 1.0
    account_refresh_seconds: float = 15.0
    clock_refresh_seconds: float = 30.0
    lease_seconds: int = 30

    @classmethod
    def from_environment(cls) -> "Settings":
        required = {
            "alpaca_api_key": os.environ.get("ALPACA_API_KEY"),
            "alpaca_secret_key": os.environ.get("ALPACA_SECRET_KEY"),
            "gcp_project_id": os.environ.get("GCP_PROJECT_ID"),
        }
        missing = [name.upper() for name, value in required.items() if not value]
        if missing:
            raise ValueError("missing environment variables: " + ", ".join(missing))
        return cls(
            alpaca_api_key=str(required["alpaca_api_key"]),
            alpaca_secret_key=str(required["alpaca_secret_key"]),
            gcp_project_id=str(required["gcp_project_id"]),
            alpaca_trading_url=os.environ.get(
                "ALPACA_TRADING_URL", "https://paper-api.alpaca.markets"
            ),
            alpaca_data_url=os.environ.get(
                "ALPACA_DATA_URL", "https://data.alpaca.markets"
            ),
        )

    def headers(self) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": self.alpaca_api_key,
            "APCA-API-SECRET-KEY": self.alpaca_secret_key,
        }
