"""
Yahoo Finance Real-time Data Source
"""

from yfinance import AsyncWebSocket


class YahooFinanceRealTimeDataSource:
    """
    A class to handle real-time data streaming from Yahoo Finance using WebSocket.
    """

    def __init__(self):
        self.ws = AsyncWebSocket()

    async def subscribe(self, symbols):
        """Subscribe to real-time data for the given symbols."""
        await self.ws.subscribe(symbols)

    async def listen(self, handle_message):
        """Listen for incoming messages and handle them using the provided callback."""
        await self.ws.listen(handle_message)

    async def close(self):
        """Close the WebSocket connection."""
        await self.ws.close()
