"""Centralised, env-driven configuration.

All tunables live here so behaviour can be changed without touching code. Values
are read once at import-time of :func:`get_config` and validated eagerly so the
bot fails fast on misconfiguration rather than mid-trade.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv is optional at runtime
    pass


def _get(name: str, default: str | None = None) -> str | None:
    val = os.getenv(name, default)
    if val is not None:
        val = val.strip()
    return val or default


def _get_bool(name: str, default: bool) -> bool:
    val = _get(name)
    if val is None:
        return default
    return val.lower() in {"1", "true", "yes", "y", "on"}


def _get_float(name: str, default: float) -> float:
    val = _get(name)
    try:
        return float(val) if val is not None else default
    except ValueError:
        return default


def _get_int(name: str, default: int) -> int:
    val = _get(name)
    try:
        return int(val) if val is not None else default
    except ValueError:
        return default


class ConfigError(RuntimeError):
    """Raised when configuration is missing or inconsistent."""


@dataclass
class TelegramConfig:
    api_id: int
    api_hash: str
    session_name: str = "gtmo_session"
    # Channel may be a numeric id (-100...), a @username, or an exact title.
    channel: str = ""
    # Optional: only one of phone (user account) is needed for Telethon login.
    phone: str | None = None


@dataclass
class MT5Config:
    login: int | None = None
    password: str | None = None
    server: str | None = None
    # Path to terminal64.exe; optional if MT5 is already running/installed.
    terminal_path: str | None = None
    symbol: str = "XAUUSD"
    # Broker symbol suffix handling, e.g. "XAUUSD.r" / "XAUUSDm".
    symbol_overrides: list[str] = field(default_factory=list)
    deviation_points: int = 30  # max slippage in points for market orders
    magic_base: int = 990000    # magic numbers are derived from this base


@dataclass
class RiskConfig:
    # Fraction of account equity risked across the whole signal (all entries).
    risk_per_signal: float = 0.01          # 1%
    max_daily_loss: float = 0.05           # 5% of start-of-day balance → halt
    max_open_signals: int = 3
    max_lot: float = 5.0
    min_lot: float = 0.01
    # If a signal has no SL we refuse to size by risk; use this fixed lot.
    fallback_lot: float = 0.01
    # Distribute risk across N positions (one per concrete TP). If false, a
    # single position is opened using the first concrete TP.
    one_position_per_tp: bool = True
    # XAUUSD pip definition in price terms (70 pips ≈ 7.0 USD move for gold).
    pip_size: float = 0.1


@dataclass
class IntelligenceConfig:
    # Signal scoring / filtering
    enabled: bool = True
    min_signal_score: float = 0.45     # skip entries scoring below this
    require_sl: bool = True            # refuse signals with no stop-loss
    min_rr: float = 0.8               # warn/penalise below this risk:reward
    require_min_rr: bool = False       # if true, also skip below min_rr
    max_chase_ratio: float = 2.0       # skip if price ran > N×SL past entry
    min_size_factor: float = 0.4       # smallest size multiplier from a take
    min_trades_for_edge: int = 15      # ignore perf edge until this many trades

    # Adaptive risk (bounded throttle off recent performance)
    adaptive_risk: bool = True
    loss_streak_throttle: int = 3      # consecutive losses before throttling
    throttle_factor: float = 0.5       # multiply risk by this when throttled
    drawdown_throttle: float = 0.08    # throttle once journal drawdown ≥ this×start
    max_size_multiplier: float = 1.0   # never scale risk above base
    min_size_multiplier: float = 0.25  # floor for the combined multiplier

    # Parser self-learning queue
    review_enabled: bool = True


@dataclass
class ExecutionConfig:
    # Trailing stop / profit lock
    trailing_enabled: bool = True
    trail_start_pips: float = 100.0     # begin trailing after this much profit
    trail_distance_pips: float = 80.0   # keep SL this far behind price
    lock_after_tp: bool = True          # move SL to entry once first TP is hit
    monitor_interval: float = 2.0       # seconds between position monitor passes
    # Spread / condition guards
    max_spread_pips: float = 0.0        # 0 = disabled; skip entries above this


@dataclass
class FiltersConfig:
    enabled: bool = False
    # Allowed trading windows as "HH:MM-HH:MM" in UTC, comma-separated.
    sessions: list = field(default_factory=list)
    # News/volatility blackout windows "YYYY-MM-DDTHH:MM/YYYY-MM-DDTHH:MM" (UTC).
    news_blackout: list = field(default_factory=list)


@dataclass
class ControlConfig:
    # File command bus (dashboard/CLI → bot).
    command_file: str = "state/commands.jsonl"
    control_file: str = "state/control.json"
    pause_file: str = "state/paused.flag"
    poll_interval: float = 1.0
    # Telegram alerts + remote control
    alerts_enabled: bool = True
    telegram_control_enabled: bool = True
    # Where to send alerts / read commands. Empty → your own Saved Messages.
    control_chat: str | None = None
    daily_summary: bool = True


@dataclass
class DashboardConfig:
    # Optional bearer token / password protecting the dashboard + control API.
    token: str | None = None


@dataclass
class ParserConfig:
    # regex | ai | hybrid
    mode: str = "hybrid"
    # gemini | anthropic
    provider: str = "gemini"
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-2.5-flash"
    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-haiku-4-5-20251001"


@dataclass
class BotConfig:
    telegram: TelegramConfig
    mt5: MT5Config
    risk: RiskConfig
    parser: ParserConfig
    intelligence: IntelligenceConfig
    execution: ExecutionConfig
    filters: FiltersConfig
    control: ControlConfig
    dashboard: DashboardConfig
    dry_run: bool = True
    log_level: str = "INFO"
    log_dir: str = "logs"
    state_file: str = "state/active_signals.json"
    status_file: str = "state/status.json"
    trades_file: str = "state/trades.jsonl"
    review_file: str = "state/review_queue.jsonl"
    symbols: list = field(default_factory=lambda: ["XAUUSD"])
    # Seconds; ignore messages older than this on startup catch-up.
    max_message_age: int = 120
    # Emergency kill switch file: if this path exists, no new trades are placed.
    emergency_stop_file: str = ".EMERGENCY_STOP"


_cached: BotConfig | None = None


def get_config(require_secrets: bool = True) -> BotConfig:
    """Build (and cache) the :class:`BotConfig` from the environment."""

    global _cached
    if _cached is not None:
        return _cached

    api_id_raw = _get("TELEGRAM_API_ID")
    api_hash = _get("TELEGRAM_API_HASH")
    channel = _get("TELEGRAM_CHANNEL", "")

    if require_secrets:
        missing = [
            n
            for n, v in (
                ("TELEGRAM_API_ID", api_id_raw),
                ("TELEGRAM_API_HASH", api_hash),
                ("TELEGRAM_CHANNEL", channel),
            )
            if not v
        ]
        if missing:
            raise ConfigError(
                "Missing required Telegram settings: " + ", ".join(missing)
            )

    telegram = TelegramConfig(
        api_id=int(api_id_raw) if api_id_raw else 0,
        api_hash=api_hash or "",
        session_name=_get("TELEGRAM_SESSION", "gtmo_session"),
        channel=channel or "",
        phone=_get("TELEGRAM_PHONE"),
    )

    overrides = _get("MT5_SYMBOL_OVERRIDES", "")
    mt5 = MT5Config(
        login=_get_int("MT5_LOGIN", 0) or None,
        password=_get("MT5_PASSWORD"),
        server=_get("MT5_SERVER"),
        terminal_path=_get("MT5_TERMINAL_PATH"),
        symbol=_get("MT5_SYMBOL", "XAUUSD"),
        symbol_overrides=[s.strip() for s in overrides.split(",") if s.strip()],
        deviation_points=_get_int("MT5_DEVIATION_POINTS", 30),
        magic_base=_get_int("MT5_MAGIC_BASE", 990000),
    )

    risk = RiskConfig(
        risk_per_signal=_get_float("RISK_PER_SIGNAL", 0.01),
        max_daily_loss=_get_float("MAX_DAILY_LOSS", 0.05),
        max_open_signals=_get_int("MAX_OPEN_SIGNALS", 3),
        max_lot=_get_float("MAX_LOT", 5.0),
        min_lot=_get_float("MIN_LOT", 0.01),
        fallback_lot=_get_float("FALLBACK_LOT", 0.01),
        one_position_per_tp=_get_bool("ONE_POSITION_PER_TP", True),
        pip_size=_get_float("PIP_SIZE", 0.1),
    )

    parser = ParserConfig(
        mode=_get("PARSER_MODE", "hybrid"),
        provider=_get("AI_PROVIDER", "gemini"),
        gemini_api_key=_get("GEMINI_API_KEY") or _get("GOOGLE_API_KEY"),
        gemini_model=_get("GEMINI_MODEL", "gemini-2.5-flash"),
        anthropic_api_key=_get("ANTHROPIC_API_KEY"),
        anthropic_model=_get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"),
    )

    intelligence = IntelligenceConfig(
        enabled=_get_bool("INTEL_ENABLED", True),
        min_signal_score=_get_float("MIN_SIGNAL_SCORE", 0.45),
        require_sl=_get_bool("REQUIRE_SL", True),
        min_rr=_get_float("MIN_RR", 0.8),
        require_min_rr=_get_bool("REQUIRE_MIN_RR", False),
        max_chase_ratio=_get_float("MAX_CHASE_RATIO", 2.0),
        min_size_factor=_get_float("MIN_SIZE_FACTOR", 0.4),
        min_trades_for_edge=_get_int("MIN_TRADES_FOR_EDGE", 15),
        adaptive_risk=_get_bool("ADAPTIVE_RISK", True),
        loss_streak_throttle=_get_int("LOSS_STREAK_THROTTLE", 3),
        throttle_factor=_get_float("THROTTLE_FACTOR", 0.5),
        drawdown_throttle=_get_float("DRAWDOWN_THROTTLE", 0.08),
        max_size_multiplier=_get_float("MAX_SIZE_MULTIPLIER", 1.0),
        min_size_multiplier=_get_float("MIN_SIZE_MULTIPLIER", 0.25),
        review_enabled=_get_bool("REVIEW_ENABLED", True),
    )

    execution = ExecutionConfig(
        trailing_enabled=_get_bool("TRAILING_ENABLED", True),
        trail_start_pips=_get_float("TRAIL_START_PIPS", 100.0),
        trail_distance_pips=_get_float("TRAIL_DISTANCE_PIPS", 80.0),
        lock_after_tp=_get_bool("LOCK_AFTER_TP", True),
        monitor_interval=_get_float("MONITOR_INTERVAL", 2.0),
        max_spread_pips=_get_float("MAX_SPREAD_PIPS", 0.0),
    )

    filters = FiltersConfig(
        enabled=_get_bool("FILTERS_ENABLED", False),
        sessions=[s.strip() for s in (_get("TRADING_SESSIONS", "") or "").split(",") if s.strip()],
        news_blackout=[s.strip() for s in (_get("NEWS_BLACKOUT", "") or "").split(",") if s.strip()],
    )

    control = ControlConfig(
        command_file=_get("COMMAND_FILE", "state/commands.jsonl"),
        control_file=_get("CONTROL_FILE", "state/control.json"),
        pause_file=_get("PAUSE_FILE", "state/paused.flag"),
        poll_interval=_get_float("CONTROL_POLL_INTERVAL", 1.0),
        alerts_enabled=_get_bool("ALERTS_ENABLED", True),
        telegram_control_enabled=_get_bool("TELEGRAM_CONTROL_ENABLED", True),
        control_chat=_get("CONTROL_CHAT"),
        daily_summary=_get_bool("DAILY_SUMMARY", True),
    )

    dashboard = DashboardConfig(token=_get("DASHBOARD_TOKEN"))

    symbols = [s.strip().upper() for s in (_get("SYMBOLS", mt5.symbol) or "XAUUSD").split(",") if s.strip()]

    cfg = BotConfig(
        telegram=telegram,
        mt5=mt5,
        risk=risk,
        parser=parser,
        intelligence=intelligence,
        execution=execution,
        filters=filters,
        control=control,
        dashboard=dashboard,
        symbols=symbols,
        dry_run=_get_bool("DRY_RUN", True),
        log_level=_get("LOG_LEVEL", "INFO"),
        log_dir=_get("LOG_DIR", "logs"),
        state_file=_get("STATE_FILE", "state/active_signals.json"),
        status_file=_get("STATUS_FILE", "state/status.json"),
        trades_file=_get("TRADES_FILE", "state/trades.jsonl"),
        review_file=_get("REVIEW_FILE", "state/review_queue.jsonl"),
        max_message_age=_get_int("MAX_MESSAGE_AGE", 120),
        emergency_stop_file=_get("EMERGENCY_STOP_FILE", ".EMERGENCY_STOP"),
    )

    _validate(cfg)
    _cached = cfg
    return cfg


def _validate(cfg: BotConfig) -> None:
    if not 0 < cfg.risk.risk_per_signal <= 0.5:
        raise ConfigError("RISK_PER_SIGNAL must be in (0, 0.5]")
    if not 0 < cfg.risk.max_daily_loss <= 1.0:
        raise ConfigError("MAX_DAILY_LOSS must be in (0, 1.0]")
    if cfg.risk.min_lot <= 0 or cfg.risk.max_lot < cfg.risk.min_lot:
        raise ConfigError("Lot bounds are invalid")
    if cfg.risk.pip_size <= 0:
        raise ConfigError("PIP_SIZE must be positive")
    if cfg.parser.mode not in {"regex", "ai", "hybrid"}:
        raise ConfigError("PARSER_MODE must be one of: regex, ai, hybrid")
    if cfg.parser.provider not in {"gemini", "anthropic", "claude"}:
        raise ConfigError("AI_PROVIDER must be one of: gemini, anthropic")


def reset_cache() -> None:
    """Testing helper to force re-read of the environment."""

    global _cached
    _cached = None
