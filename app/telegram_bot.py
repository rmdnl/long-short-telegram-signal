"""
Telegram bot: sends signals to Telegram.

Uses python-telegram-bot library via its Bot API wrapper.

PHASE 9 responsibilities:
- TELEGRAM_ENABLED controls whether real network calls happen (default false).
- When disabled: no network call, signal still processed logically, log message
  that *would* be sent.
- When enabled: POST to https://api.telegram.org/bot<TOKEN>/sendMessage
  with bounded retry/backoff (no unbounded retry).
- Never logs the token or the Authorization header.
- Signal-only: never performs trading execution.
"""
import time
from typing import List, Optional

import requests

from app.models import Signal, SignalDirection, MarketBias
from app.config import get_config, ConfigValidationError
from app.logger import get_logger

logger = get_logger(__name__)

# Telegram Bot API endpoint template (base only — token attached at call time)
_TELEGRAM_API_BASE = "https://api.telegram.org/bot"
_SEND_MESSAGE_PATH = "/sendMessage"


class TelegramBotError(Exception):
    """Raised when Telegram delivery fails irrecoverably."""
    pass


class TelegramConfigError(TelegramBotError):
    """Raised when Telegram is enabled but required config is missing."""
    pass


def _is_transient_http(status_code: int) -> bool:
    """Return True for status codes that are worth retrying."""
    return status_code in (429, 500, 502, 503, 504)


class TelegramBot:
    """
    Telegram message sender for signal delivery.

    Config (all from environment — never hardcoded):
        TELEGRAM_BOT_TOKEN   — required when TELEGRAM_ENABLED=true
        TELEGRAM_CHAT_ID     — required when TELEGRAM_ENABLED=true
        TELEGRAM_ENABLED     — default false; master switch for network delivery
    """

    def __init__(self):
        self.config = get_config()
        # Bot identity is resolved lazily / on-demand so that simply
        # instantiating TelegramBot never raises when disabled.

    # ------------------------------------------------------------------
    # Config helpers
    # ------------------------------------------------------------------
    def _resolve_credentials(self) -> tuple[str, str]:
        """
        Resolve token + chat_id from config. Raises TelegramConfigError
        when TELEGRAM_ENABLED is true but config is missing/invalid.
        """
        if not self.config.telegram_enabled:
            raise TelegramConfigError(
                "TELEGRAM_ENABLED is false; credentials not required"
            )
        try:
            token = self.config.telegram_bot_token
            chat_id = self.config.telegram_chat_id
        except ConfigValidationError as e:
            raise TelegramConfigError(str(e))
        return token, chat_id

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def send_signal(self, signal: Signal) -> bool:
        """
        Format and send signal to Telegram.

        Returns:
            True if sent successfully (or would be sent in dry-run).
            False if delivery failed after bounded retries.

        Behavior:
        - TELEGRAM_ENABLED=false → log what would be sent, return True, no network.
        - TELEGRAM_ENABLED=true  → POST to Telegram API with bounded retry.
        - On failure: log error, return False. Caller (scanner/main) continues;
          the signal is still saved to the local signal store.
        """
        message = self._format_signal(signal)

        if not self.config.telegram_enabled:
            # Dry-run: no network call. Log the message that *would* be sent.
            logger.info(
                f"[TELEGRAM DISABLED] Would send signal: {signal.signal_id}\n"
                f"--- message start ---\n{message}\n--- message end ---"
            )
            return True

        # Enabled: validate credentials, then attempt delivery.
        try:
            token, chat_id = self._resolve_credentials()
        except TelegramConfigError as e:
            logger.error(f"Telegram config error, not sending: {e}")
            return False

        try:
            self._send_with_retry(token, chat_id, message)
        except TelegramBotError as e:
            # Bounded retry exhausted or fatal error: log without raising,
            # so the scanner never crashes on a Telegram failure.
            logger.error(
                f"Telegram delivery failed for {signal.signal_id} "
                f"after bounded retries: {e}"
            )
            return False

        logger.info(f"Telegram message sent: {signal.signal_id}")
        return True

    # ------------------------------------------------------------------
    # Network layer (bounded retry / backoff)
    # ------------------------------------------------------------------
    def _send_with_retry(
        self,
        token: str,
        chat_id: str,
        text: str,
        max_retries: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 60.0,
    ) -> None:
        """
        Send a message via the Telegram Bot API with bounded retry/backoff.

        - Retries only on transient HTTP statuses (429, 5xx) or network errors.
        - 429 honors Retry-After header.
        - Never retries indefinitely; aborts after max_retries.
        - Does NOT log the token or Authorization header at any point.
        """
        url = f"{_TELEGRAM_API_BASE}{token}{_SEND_MESSAGE_PATH}"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown",
        }
        # Never set Authorization header explicitly (Bot API uses URL token).
        # We deliberately do NOT log `url`, `token`, or `payload` here.
        headers = {"User-Agent": "crypto-signal-bot/1.0"}

        last_error: Optional[str] = None
        for attempt in range(1, max_retries + 1):
            try:
                resp = requests.post(
                    url,
                    data=payload,
                    headers=headers,
                    timeout=15,
                )
                status = resp.status_code

                if status == 200:
                    return  # success

                if not _is_transient_http(status):
                    # Non-retryable: e.g. 400 bad request, 401 unauthorized.
                    logger.warning(
                        f"Telegram non-retryable HTTP {status} for signal; "
                        f"response suppressed (no secrets)."
                    )
                    raise TelegramBotError(
                        f"Telegram API returned non-retryable HTTP {status}"
                    )

                last_error = f"HTTP {status}"

                # 429: honor Retry-After if present
                if status == 429:
                    retry_after = resp.headers.get("Retry-After")
                    if retry_after:
                        try:
                            delay = float(retry_after)
                        except ValueError:
                            delay = base_delay * (2 ** (attempt - 1))
                    else:
                        delay = base_delay * (2 ** (attempt - 1))
                else:
                    # Exponential backoff for transient 5xx
                    delay = base_delay * (2 ** (attempt - 1))

                delay = min(delay, max_delay)
                logger.warning(
                    f"Telegram transient HTTP {status}; retry {attempt}/{max_retries} "
                    f"after {delay:.1f}s"
                )
                time.sleep(delay)

            except requests.exceptions.RequestException as e:
                last_error = f"network error: {type(e).__name__}"
                delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                logger.warning(
                    f"Telegram network error; retry {attempt}/{max_retries} "
                    f"after {delay:.1f}s"
                )
                time.sleep(delay)

        # Exhausted all retries.
        raise TelegramBotError(
            f"Telegram delivery failed after {max_retries} attempts ({last_error})"
        )

    # ------------------------------------------------------------------
    # Message formatting
    # ------------------------------------------------------------------
    def _format_signal(self, signal: Signal) -> str:
        """
        Format a signal as a Telegram message.

        Uses the PHASE 9 message specification. Scores are presented as
        quality scores /100 — never as "probability".
        """
        direction = signal.direction
        direction_text = "LONG" if direction == SignalDirection.LONG else "SHORT"
        direction_emoji = "🚨"

        entry_mid = (signal.entry_low + signal.entry_high) / 2
        risk = abs(entry_mid - signal.stop_loss)
        reward_tp2 = abs(signal.tp2 - entry_mid)
        rr_display = (reward_tp2 / risk) if risk > 0 else 0

        ema_fast = self.config.ema_fast
        ema_slow = self.config.ema_slow
        bias = signal.htf_bias
        bias_text = "Bullish" if bias == MarketBias.BULLISH else "Bearish"

        # EMA confirmation
        if direction == SignalDirection.LONG:
            ema_check = f"EMA{ema_fast} > EMA{ema_slow} ✓"
        else:
            ema_check = f"EMA{ema_fast} < EMA{ema_slow} ✓"

        # 1H trend
        h1_trend = "Bullish" if bias == MarketBias.BULLISH else "Bearish"

        # 15M trend
        if direction == SignalDirection.LONG:
            tf15_trend = f"EMA{ema_fast} > EMA{ema_slow} ✓"
        else:
            tf15_trend = f"EMA{ema_fast} < EMA{ema_slow} ✓"

        setup_name = signal.signal_type.value.replace("_", " ")

        message = f"""{direction_emoji} **{direction_text}**

`{signal.symbol}`
`TIMEFRAME: 5M`

Entry:
`{signal.entry_low:.2f} - {signal.entry_high:.2f}`

SL:
`{signal.stop_loss:.2f}`

TP1:
`{signal.tp1:.2f}`

TP2:
`{signal.tp2:.2f}`

RR:
`{rr_display:.1f}`

Score:
`{signal.score}/100`

Setup:
`{setup_name}`

Confirmation:
* 1H trend: {h1_trend}
* 15M trend: {tf15_trend}
* ADX / DI: {signal.adx_value:.1f}
* RSI: {signal.rsi_value:.1f}
* 5M trigger: breakout confirmed
* Volume: {signal.volume_ratio:.2f}x average

Invalidation:
`{"15M close below entry zone" if direction == SignalDirection.LONG else "15M close above entry zone"}`

Signal only • No auto trading"""
        return message


# ------------------------------------------------------------------
    # Public API: startup & outcome notifications
    # ------------------------------------------------------------------
    def send_startup(
        self,
        symbols: List[str],
        interval_seconds: int,
        min_score: int,
        components: dict,
    ) -> bool:
        """Send the single 🚀 BOT ONLINE message after successful init.

        Returns True on success (or dry-run), False on failure after bounded
        retries. Uses the same retry policy as send_signal().
        """
        from app.outcome_monitor import format_startup_notification

        message = format_startup_notification(
            symbols=symbols,
            interval_seconds=interval_seconds,
            min_score=min_score,
            telegram_enabled=self.config.telegram_enabled,
            components=components,
        )

        if not self.config.telegram_enabled:
            logger.info(
                f"[TELEGRAM DISABLED] Would send startup:\n--- message start ---\n"
                f"{message}\n--- message end ---"
            )
            return True

        try:
            token, chat_id = self._resolve_credentials()
        except TelegramConfigError as e:
            logger.error(f"Telegram config error, not sending startup: {e}")
            return False

        try:
            self._send_with_retry(token, chat_id, message)
        except TelegramBotError as e:
            logger.error(f"Telegram startup notification failed: {e}")
            return False

        logger.info("Startup notification sent")
        return True

    def send_outcome(self, signal: Signal, level: str) -> bool:
        """Send a TP1 / TP2 / SL notification for an already-delivered signal.

        level must be one of "TP1", "TP2", "SL".
        """
        from app.outcome_monitor import format_outcome

        message = format_outcome(signal, level)

        if not self.config.telegram_enabled:
            logger.info(
                f"[TELEGRAM DISABLED] Would send outcome {level} "
                f"for {signal.signal_id}"
            )
            return True

        try:
            token, chat_id = self._resolve_credentials()
        except TelegramConfigError as e:
            logger.error(f"Telegram config error, not sending outcome: {e}")
            return False

        try:
            self._send_with_retry(token, chat_id, message)
        except TelegramBotError as e:
            logger.error(f"Telegram outcome notification failed: {e}")
            return False

        logger.info(f"Outcome notification {level} sent for {signal.signal_id}")
        return True


# Module-level convenience used by main.py / scanner wiring
def send_signal(signal: Signal) -> bool:
    """
    Convenience wrapper: instantiate TelegramBot and send.
    Used by callers that just want the single method call.
    """
    bot = TelegramBot()
    return bot.send_signal(signal)
