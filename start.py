"""Launcher: ensures an event loop exists (Python 3.13+), then starts the bot."""
import asyncio

try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

import bot
bot.main()
