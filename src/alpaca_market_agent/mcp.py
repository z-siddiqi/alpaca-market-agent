import os
from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from alpaca_market_agent.config import Settings

READ_TOOLS = {
    "get_open_position",
    "get_option_chain",
    "get_option_contract",
    "get_option_contracts",
    "get_option_latest_quote",
    "get_option_snapshot",
    "get_order_by_client_id",
    "get_order_by_id",
}
WRITE_TOOLS = {
    "cancel_order_by_id",
    "close_position",
    "place_option_order",
    "replace_order_by_id",
}
ALLOWED_TOOLS = READ_TOOLS | WRITE_TOOLS


class AlpacaMcpClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self._tools: list[dict[str, Any]] = []

    async def __aenter__(self) -> "AlpacaMcpClient":
        if self.settings.alpaca_api_key is None or self.settings.alpaca_secret_key is None:
            raise ValueError("ALPACA_API_KEY and ALPACA_SECRET_KEY are required")

        environment = {
            "PATH": os.environ.get("PATH", ""),
            "ALPACA_API_KEY": self.settings.alpaca_api_key.get_secret_value(),
            "ALPACA_SECRET_KEY": self.settings.alpaca_secret_key.get_secret_value(),
            "ALPACA_PAPER_TRADE": str(self.settings.alpaca_paper_trade).lower(),
            "ALPACA_TOOLSETS": "trading,assets,options-data",
        }
        self._stack = AsyncExitStack()
        read, write = await self._stack.enter_async_context(
            stdio_client(StdioServerParameters(command="alpaca-mcp-server", env=environment))
        )
        self._session = await self._stack.enter_async_context(ClientSession(read, write))
        await self._session.initialize()
        listed = await self._session.list_tools()
        self._tools = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description or "",
                    "parameters": tool.inputSchema,
                },
            }
            for tool in listed.tools
            if tool.name in ALLOWED_TOOLS
        ]
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._stack is not None:
            await self._stack.aclose()

    @property
    def tools(self) -> list[dict[str, Any]]:
        return self._tools

    async def call(self, name: str, arguments: dict[str, Any]) -> tuple[Any, bool]:
        if name not in ALLOWED_TOOLS:
            return {"error": f"tool {name} is not available"}, True
        if name in WRITE_TOOLS and not self.settings.order_submission_enabled:
            return {
                "error": "order submission is disabled",
                "submissionEnabled": False,
            }, True
        if self._session is None:
            raise RuntimeError("Alpaca MCP client is not connected")
        result = await self._session.call_tool(name, arguments)
        return result.model_dump(mode="json", by_alias=True), False
