"""
Read-only monitoring dashboard for the Crypto Long/Short Signal Bot.

This package is an OBSERVER. It sits beside the signal bot and reads the
bot's existing SQLite database and configuration. It is deliberately isolated
from the decision engine:

* No signal generation.
* No indicator, risk, or scanner logic.
* No order routing of any kind.
* No credential handling beyond optional dashboard login.
* No writes to the bot database, bot state, or Telegram state.

The dashboard never imports the signal engine, scanner, risk engine, market
data, Telegram client, or outcome monitor. It only reads persisted data.
"""

__version__ = "1.0.0"
__all__ = ["__version__"]
