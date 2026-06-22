"""
Stock Portfolio Tracker CLI (full-screen interactive TUI).

Install:
    pip install textual yfinance

Run:
    python portfolio.py

Key bindings:
    1 / 2 / 3     Switch tabs (Positions / Market Viewer / Allocation)
    r             Refresh data now
    q             Quit
    a             Add position (Positions tab)
    up / down     Move selection between positions (Positions tab)
    e             Edit selected position (Positions tab)
    d             Delete selected position (Positions tab)
    s             Save holdings JSON (Positions tab)
    /             Focus ticker input (Market Viewer tab)
    Click 1D-ALL  Change chart timeframe (Market Viewer tab)
    left / right  Previous / next timeframe (Market Viewer tab)
    4 .. 0, -     Jump to timeframe (4=1D, 5=5D, 6=1M, 7=3M, 8=6M, 9=1Y, 0=5Y, -=ALL)
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import yfinance as yf
from rich.columns import Columns
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, Static, TabbedContent, TabPane

DATA_FILE = Path(__file__).with_name("portfolioData.json")
CATEGORIES = ("Offensive", "Neutral", "Defensive")
CATEGORY_COLORS = {
    "Offensive": "bold red",
    "Neutral": "bold yellow",
    "Defensive": "bold green",
}
TIMEFRAME_MAP = {
    "1D": ("1d", "5m"),
    "5D": ("5d", "30m"),
    "1M": ("1mo", "1d"),
    "3M": ("3mo", "1d"),
    "6M": ("6mo", "1d"),
    "1Y": ("1y", "1d"),
    "5Y": ("5y", "1wk"),
    "ALL": ("max", "1wk"),
}
TIMEFRAME_OPTIONS = tuple(TIMEFRAME_MAP.keys())


def timeframe_button_id(timeframe: str) -> str:
    return f"tf_btn_{timeframe.lower()}"


@dataclass
class Position:
    ticker: str
    category: str
    sector: str
    shares: float
    cost_basis: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "category": self.category,
            "sector": self.sector,
            "shares": self.shares,
            "cost_basis": self.cost_basis,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Position":
        return cls(
            ticker=str(data.get("ticker", "")).strip().upper(),
            category=normalize_category(str(data.get("category", "Neutral"))),
            sector=str(data.get("sector", "")).strip() or "Unknown",
            shares=float(data.get("shares", 0.0)),
            cost_basis=float(data.get("cost_basis", 0.0)),
        )


def normalize_category(raw: str) -> str:
    normalized = raw.strip().lower()
    if normalized in ("offensive", "aggressive"):
        return "Offensive"
    if normalized in ("defensive", "conservative"):
        return "Defensive"
    return "Neutral"


def format_money(value: float) -> str:
    return f"${value:,.2f}"


def format_percent(value: float) -> str:
    return f"{value:,.2f}%"


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def default_data() -> dict[str, Any]:
    return {
        "positions": [],
        "viewer": {
            "ticker": "AAPL",
            "timeframe": "1M",
        },
    }


def load_data(path: Path) -> dict[str, Any]:
    if not path.exists():
        initial = default_data()
        path.write_text(json.dumps(initial, indent=2), encoding="utf-8")
        return initial
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        payload = default_data()
    payload.setdefault("positions", [])
    payload.setdefault("viewer", {})
    payload["viewer"].setdefault("ticker", "AAPL")
    payload["viewer"].setdefault("timeframe", "1M")
    return payload


def save_data(path: Path, positions: list[Position], viewer_ticker: str, viewer_timeframe: str) -> None:
    payload = {
        "positions": [position.to_dict() for position in positions],
        "viewer": {"ticker": viewer_ticker, "timeframe": viewer_timeframe},
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def last_close_price(symbol: str) -> Optional[float]:
    ticker = yf.Ticker(symbol)
    history = ticker.history(period="5d", interval="1d", auto_adjust=False)
    if history.empty or "Close" not in history:
        return None
    closes = history["Close"].dropna()
    if closes.empty:
        return None
    return safe_float(closes.iloc[-1], default=None)


def validate_ticker(symbol: str) -> tuple[bool, str, str]:
    try:
        ticker = yf.Ticker(symbol)
        history = ticker.history(period="5d", interval="1d", auto_adjust=False)
        if history.empty:
            return False, "", f"Ticker '{symbol}' did not return market data."
        sector = ""
        try:
            info = ticker.get_info()
            sector = str(info.get("sector", "")).strip()
        except Exception:
            sector = ""
        return True, sector, ""
    except Exception as exc:
        return False, "", f"Ticker validation failed: {exc}"


def market_snapshot(symbol: str, timeframe: str) -> dict[str, Any]:
    period, interval = TIMEFRAME_MAP.get(timeframe, TIMEFRAME_MAP["1M"])
    ticker = yf.Ticker(symbol)

    info: dict[str, Any] = {}
    try:
        info = ticker.get_info()
    except Exception:
        info = {}

    history = ticker.history(period=period, interval=interval, auto_adjust=False)
    closes = history["Close"].dropna() if not history.empty and "Close" in history else []
    chart_points: list[tuple[datetime, float]] = []
    if hasattr(closes, "items"):
        for dt, value in closes.items():
            chart_points.append((dt.to_pydatetime(), safe_float(value)))

    current_price = None
    prev_close = None
    if hasattr(closes, "iloc") and len(closes) > 0:
        current_price = safe_float(closes.iloc[-1], default=None)
        prev_close = safe_float(closes.iloc[-2], default=current_price) if len(closes) > 1 else current_price
    if current_price is None:
        current_price = safe_float(info.get("regularMarketPrice"), default=None)
    if prev_close is None:
        prev_close = safe_float(info.get("regularMarketPreviousClose"), default=current_price)

    change_value = 0.0
    change_pct = 0.0
    if current_price is not None and prev_close not in (None, 0):
        change_value = current_price - prev_close
        change_pct = (change_value / prev_close) * 100

    return {
        "symbol": symbol,
        "name": info.get("shortName") or info.get("longName") or symbol,
        "sector": info.get("sector") or "N/A",
        "market_cap": info.get("marketCap"),
        "pe": info.get("trailingPE"),
        "week52_low": info.get("fiftyTwoWeekLow"),
        "week52_high": info.get("fiftyTwoWeekHigh"),
        "volume": info.get("volume"),
        "current_price": current_price,
        "change_value": change_value,
        "change_pct": change_pct,
        "chart_points": chart_points,
    }


def format_axis_price(value: float) -> str:
    abs_val = abs(value)
    if abs_val >= 1_000_000:
        return f"${value / 1_000_000:.1f}M"
    if abs_val >= 10_000:
        return f"${value / 1_000:.1f}K"
    if abs_val >= 1_000:
        return f"${value:,.0f}"
    if abs_val >= 100:
        return f"${value:.1f}"
    return f"${value:.2f}"


def format_axis_time(dt: datetime, timeframe: str) -> str:
    if timeframe in ("1D", "5D"):
        return dt.strftime("%H:%M")
    if timeframe in ("1M", "3M", "6M"):
        return dt.strftime("%b %d")
    return dt.strftime("%b '%y")


BRAILLE_DOTS = {
    (0, 0): 0x01,
    (0, 1): 0x02,
    (0, 2): 0x04,
    (0, 3): 0x40,
    (1, 0): 0x08,
    (1, 1): 0x10,
    (1, 2): 0x20,
    (1, 3): 0x80,
}


class BrailleCanvas:
    def __init__(self, width: int, height: int) -> None:
        self.width = max(1, width)
        self.height = max(1, height)
        self.cells = [[0 for _ in range(self.width)] for _ in range(self.height)]

    def set_dot(self, x: int, y: int) -> None:
        if x < 0 or y < 0:
            return
        char_x = x // 2
        char_y = y // 4
        if char_x >= self.width or char_y >= self.height:
            return
        self.cells[char_y][char_x] |= BRAILLE_DOTS[(x % 2, y % 4)]

    def line(self, x0: int, y0: int, x1: int, y1: int) -> None:
        dx = abs(x1 - x0)
        dy = -abs(y1 - y0)
        sx = 1 if x0 < x1 else -1
        sy = 1 if y0 < y1 else -1
        err = dx + dy
        x, y = x0, y0
        while True:
            self.set_dot(x, y)
            if x == x1 and y == y1:
                break
            err2 = 2 * err
            if err2 >= dy:
                err += dy
                x += sx
            if err2 <= dx:
                err += dx
                y += sy


def resample_points(points: list[tuple[datetime, float]], target_count: int) -> list[tuple[datetime, float]]:
    if len(points) <= target_count or target_count < 2:
        return points
    step = (len(points) - 1) / (target_count - 1)
    sampled: list[tuple[datetime, float]] = []
    for index in range(target_count):
        sample_index = int(round(index * step))
        sampled.append(points[sample_index])
    return sampled


def build_chart(
    points: list[tuple[datetime, float]],
    timeframe: str,
    symbol: str,
    width: int,
    height: int,
    prev_close: Optional[float] = None,
) -> Group | Text:
    if not points:
        return Text("No chart data available for the selected timeframe.", style="dim")

    prices = [point[1] for point in points]
    start_price = prices[0]
    end_price = prices[-1]
    is_profit = end_price >= start_price
    line_style = "bold bright_green" if is_profit else "bold bright_red"
    ref_style = "dim"

    y_label_width = 9
    plot_width = max(24, width - y_label_width - 2)
    plot_height = max(8, height - 3)

    price_min = min(prices)
    price_max = max(prices)
    if prev_close is not None:
        price_min = min(price_min, prev_close)
        price_max = max(price_max, prev_close)

    padding = (price_max - price_min) * 0.06 or max(abs(end_price) * 0.02, 0.5)
    price_min -= padding
    price_max += padding
    price_span = price_max - price_min or 1.0

    dot_width = plot_width * 2
    dot_height = plot_height * 4
    sampled = resample_points(points, dot_width)

    def price_to_y(price: float) -> int:
        ratio = (price - price_min) / price_span
        return int(round((1.0 - ratio) * (dot_height - 1)))

    main_canvas = BrailleCanvas(plot_width, plot_height)
    ref_canvas = BrailleCanvas(plot_width, plot_height)

    if prev_close is not None and timeframe == "1D":
        ref_y = price_to_y(prev_close)
        for x in range(dot_width):
            ref_canvas.set_dot(x, ref_y)

    for index in range(1, len(sampled)):
        x0 = index - 1
        x1 = index
        y0 = price_to_y(sampled[index - 1][1])
        y1 = price_to_y(sampled[index][1])
        main_canvas.line(x0, y0, x1, y1)

    y_ticks = [price_max - (price_span * step / (plot_height - 1)) for step in range(plot_height)]
    if plot_height == 1:
        y_ticks = [price_max]

    chart_rows: list[Text] = []
    for row in range(plot_height):
        label = format_axis_price(y_ticks[row]) if row < len(y_ticks) else ""
        row_text = Text()
        row_text.append(label.rjust(y_label_width), style="dim")
        row_text.append("│", style="dim")
        for col in range(plot_width):
            main_bits = main_canvas.cells[row][col]
            ref_bits = ref_canvas.cells[row][col]
            if main_bits:
                row_text.append(chr(0x2800 + main_bits), style=line_style)
            elif ref_bits:
                row_text.append(chr(0x2800 + ref_bits), style=ref_style)
            else:
                row_text.append(" ")
        chart_rows.append(row_text)

    x_labels = _chart_x_labels(sampled, timeframe, plot_width)
    axis_line = Text()
    axis_line.append(" " * y_label_width, style="dim")
    axis_line.append("└", style="dim")
    axis_line.append("─" * plot_width, style="dim")

    label_line = Text()
    label_line.append(" " * (y_label_width + 1), style="dim")
    if x_labels:
        buffer = [" "] * plot_width
        if len(x_labels) == 1:
            label = x_labels[0]
            start = max(0, (plot_width - len(label)) // 2)
            for index, character in enumerate(label):
                if start + index < plot_width:
                    buffer[start + index] = character
        else:
            for index, label in enumerate(x_labels):
                start = int(round(index * (plot_width - len(label)) / (len(x_labels) - 1)))
                for offset, character in enumerate(label):
                    if start + offset < plot_width:
                        buffer[start + offset] = character
        label_line.append("".join(buffer), style="dim")

    header_parts: list[tuple[str, str]] = [
        (f" {symbol} ", "bold"),
        (f" {timeframe} ", "dim"),
        (format_money(start_price), "dim"),
        (" -> ", "dim"),
        (format_money(end_price), line_style),
        (
            f"  ({'+' if end_price >= start_price else ''}{format_money(end_price - start_price)}, "
            f"{'+' if end_price >= start_price else ''}{format_percent(((end_price - start_price) / start_price) * 100 if start_price else 0.0)})",
            line_style,
        ),
    ]
    if prev_close is not None and timeframe == "1D":
        header_parts.extend([("  prev ", "dim"), (format_money(prev_close), ref_style)])
    header = Text.assemble(*header_parts)

    return Group(
        header,
        Text("─" * (y_label_width + plot_width + 1), style="dim"),
        *chart_rows,
        axis_line,
        label_line,
    )


def _chart_x_labels(
    points: list[tuple[datetime, float]],
    timeframe: str,
    plot_width: int,
) -> list[str]:
    if not points:
        return []
    label_count = min(5, max(2, plot_width // 14))
    if len(points) == 1:
        return [format_axis_time(points[0][0], timeframe)]
    labels: list[str] = []
    for index in range(label_count):
        point_index = int(round(index * (len(points) - 1) / (label_count - 1)))
        labels.append(format_axis_time(points[point_index][0], timeframe))
    return labels


def alloc_bar(percent: float, width: int = 24) -> str:
    percent = max(0.0, min(100.0, percent))
    filled = int(round((percent / 100.0) * width))
    return ("#" * filled) + ("-" * (width - filled))


class TerminalDialogScreen(ModalScreen):
    """Base modal with dimmed backdrop and heavy ASCII-style border."""

    DEFAULT_CSS = """
    TerminalDialogScreen {
        align: center middle;
    }
    TerminalDialogScreen > .dialog_box {
        width: 72;
        height: auto;
        border: heavy $primary;
        background: $surface;
        padding: 0 1 1 1;
    }
    TerminalDialogScreen .dialog_title {
        width: 100%;
        text-align: center;
        padding: 0 0 1 0;
        text-style: bold;
    }
    TerminalDialogScreen .field_row {
        height: auto;
        margin-bottom: 1;
    }
    TerminalDialogScreen .field_label {
        width: 14;
        content-align: left middle;
        color: $text-muted;
    }
    TerminalDialogScreen .field_row Input {
        width: 1fr;
    }
    TerminalDialogScreen .dialog_hint {
        color: $text-muted;
        text-style: dim;
        padding-top: 1;
        border-top: heavy $primary-darken-2;
    }
    TerminalDialogScreen .dialog_error {
        color: $error;
        height: auto;
        min-height: 1;
    }
    """


class PositionEditorScreen(TerminalDialogScreen):
    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("enter", "submit", "Save", show=False),
    ]

    FIELD_IDS = ("pos_ticker", "pos_category", "pos_sector", "pos_shares", "pos_cost_basis")

    def __init__(self, initial: Optional[Position] = None) -> None:
        super().__init__()
        self.initial = initial

    def compose(self) -> ComposeResult:
        title = "edit position" if self.initial else "add position"
        ticker = self.initial.ticker if self.initial else ""
        category = self.initial.category if self.initial else "Neutral"
        sector = self.initial.sector if self.initial else ""
        shares = f"{self.initial.shares:.6f}".rstrip("0").rstrip(".") if self.initial else ""
        cost_basis = f"{self.initial.cost_basis:.4f}".rstrip("0").rstrip(".") if self.initial else ""

        with Container(classes="dialog_box"):
            yield Static(f"─ {title} ─", classes="dialog_title")
            with Horizontal(classes="field_row"):
                yield Static("ticker", classes="field_label")
                yield Input(value=ticker, id="pos_ticker", placeholder="AAPL")
            with Horizontal(classes="field_row"):
                yield Static("category", classes="field_label")
                yield Input(value=category, id="pos_category", placeholder="Offensive / Neutral / Defensive")
            with Horizontal(classes="field_row"):
                yield Static("sector", classes="field_label")
                yield Input(value=sector, id="pos_sector", placeholder="optional")
            with Horizontal(classes="field_row"):
                yield Static("shares", classes="field_label")
                yield Input(value=shares, id="pos_shares", placeholder="10.5")
            with Horizontal(classes="field_row"):
                yield Static("cost basis", classes="field_label")
                yield Input(value=cost_basis, id="pos_cost_basis", placeholder="180.25")
            yield Static("tab next field · enter save · esc cancel", classes="dialog_hint")
            yield Static("", id="dialog_error", classes="dialog_error")

    def on_mount(self) -> None:
        self.query_one("#pos_ticker", Input).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_submit(self) -> None:
        self._submit_form()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id not in self.FIELD_IDS:
            return
        self._submit_form()

    def _validate_form(self) -> str:
        ticker = self.query_one("#pos_ticker", Input).value.strip().upper()
        shares_raw = self.query_one("#pos_shares", Input).value.strip()
        cost_basis_raw = self.query_one("#pos_cost_basis", Input).value.strip()

        if not ticker:
            return "ticker is required"
        if not shares_raw:
            return "shares are required"
        if not cost_basis_raw:
            return "cost basis is required"
        try:
            shares = float(shares_raw)
            cost_basis = float(cost_basis_raw)
        except ValueError:
            return "shares and cost basis must be numbers"
        if shares <= 0:
            return "shares must be greater than zero"
        if cost_basis < 0:
            return "cost basis must be zero or greater"
        return ""

    def _submit_form(self) -> None:
        error = self._validate_form()
        error_widget = self.query_one("#dialog_error", Static)
        if error:
            error_widget.update(error)
            return
        error_widget.update("")
        payload = {
            "ticker": self.query_one("#pos_ticker", Input).value.strip().upper(),
            "category": self.query_one("#pos_category", Input).value.strip(),
            "sector": self.query_one("#pos_sector", Input).value.strip(),
            "shares": self.query_one("#pos_shares", Input).value.strip(),
            "cost_basis": self.query_one("#pos_cost_basis", Input).value.strip(),
        }
        self.dismiss(payload)


class PortfolioTrackerApp(App[None]):
    TITLE = "Stock Portfolio Tracker"

    BINDINGS = [
        Binding("1", "tab_positions", "Positions", priority=True),
        Binding("2", "tab_viewer", "Market", priority=True),
        Binding("3", "tab_allocation", "Allocation", priority=True),
        Binding("r", "refresh_now", "Refresh"),
        Binding("q", "quit", "Quit"),
        Binding("a", "add_position", "Add"),
        Binding("e", "edit_position", "Edit"),
        Binding("d", "delete_position", "Delete"),
        Binding("y", "status_prompt_y", "Yes", show=False),
        Binding("n", "status_prompt_n", "No", show=False),
        Binding("s", "save_portfolio", "Save"),
        Binding("/", "focus_ticker", "Ticker"),
        Binding("escape", "blur_input", "Unfocus", show=False),
        Binding("left", "tf_prev", "< TF", show=False),
        Binding("right", "tf_next", "TF >", show=False),
        Binding("4", "tf_1d", "1D", show=False),
        Binding("5", "tf_5d", "5D", show=False),
        Binding("6", "tf_1m", "1M", show=False),
        Binding("7", "tf_3m", "3M", show=False),
        Binding("8", "tf_6m", "6M", show=False),
        Binding("9", "tf_1y", "1Y", show=False),
        Binding("0", "tf_5y", "5Y", show=False),
        Binding("-", "tf_all", "ALL", show=False),
    ]

    CSS = """
    Header HeaderIcon {
        display: none;
    }
    #positions_status {
        height: auto;
        padding: 0 1;
    }
    #positions_table {
        height: 1fr;
    }
    #viewer_container {
        height: 1fr;
    }
    #viewer_top {
        height: auto;
        margin: 0 0 1 0;
    }
    #viewer_timeframes {
        height: auto;
        margin: 0 0 1 0;
    }
    #viewer_timeframes Button {
        min-width: 5;
        margin-right: 1;
    }
    #ticker_input {
        width: 24;
        margin-right: 1;
    }
    #viewer_summary {
        width: 1fr;
        height: auto;
    }
    #viewer_chart {
        height: 1fr;
        padding: 0 1;
    }
    #allocation_view {
        height: 1fr;
    }
    #status_bar {
        dock: bottom;
        height: 1;
        background: $surface-darken-1;
        color: $text-muted;
        padding: 0 1;
    }
    #status_bar.prompt {
        color: $warning;
        text-style: bold;
    }
    #status_bar.error {
        color: $error;
    }
    #status_bar.success {
        color: $success;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self.positions: list[Position] = []
        self.price_cache: dict[str, Optional[float]] = {}
        self.viewer_snapshot: dict[str, Any] = {}
        self.viewer_ticker = "AAPL"
        self.viewer_timeframe = "1M"
        self.row_to_position: list[int] = []
        self.selected_position_row = 0
        self.last_refresh_request = 0.0
        self.refresh_in_progress = False
        self.last_refresh_label = "Never"
        self.status_prompt: Optional[dict[str, Callable[[], None]]] = None
        self._status_clear_timer = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with TabbedContent(id="tabs", initial="positions"):
            with TabPane("Positions", id="positions"):
                yield Static("Loading positions...", id="positions_status")
                yield DataTable(id="positions_table", cursor_type="row", zebra_stripes=True)
            with TabPane("Market Viewer", id="viewer"):
                with Vertical(id="viewer_container"):
                    with Horizontal(id="viewer_top"):
                        yield Input(placeholder="Ticker and Enter", id="ticker_input")
                        yield Static("", id="viewer_summary")
                    with Horizontal(id="viewer_timeframes"):
                        for timeframe in TIMEFRAME_OPTIONS:
                            yield Button(timeframe, id=timeframe_button_id(timeframe))
                    yield Static("", id="viewer_chart")
            with TabPane("Allocation", id="allocation"):
                yield Static("", id="allocation_view")
            with TabPane("Allocation", id="allocation"):
                yield Static("", id="allocation_view")
        yield Static("", id="status_bar")
        yield Footer()

    def _cancel_status_timer(self) -> None:
        if self._status_clear_timer is not None:
            self._status_clear_timer.stop()
            self._status_clear_timer = None

    def clear_status(self) -> None:
        if self.status_prompt:
            return
        bar = self.query_one("#status_bar", Static)
        bar.update("")
        bar.remove_class("prompt", "error", "success")
        self._status_clear_timer = None

    def show_status(self, message: str, level: str = "info", timeout: float = 4.0) -> None:
        if self.status_prompt:
            return
        self._cancel_status_timer()
        bar = self.query_one("#status_bar", Static)
        bar.remove_class("prompt", "error", "success")
        if level == "error":
            bar.add_class("error")
            text = f" ! {message}"
        elif level == "success":
            bar.add_class("success")
            text = f" OK {message}"
        else:
            text = f" · {message}"
        bar.update(text)
        if timeout > 0:
            self._status_clear_timer = self.set_timer(timeout, self.clear_status)

    def begin_status_prompt(self, message: str, on_confirm: Callable[[], None]) -> None:
        self._cancel_status_timer()
        self.status_prompt = {"on_confirm": on_confirm}
        bar = self.query_one("#status_bar", Static)
        bar.remove_class("error", "success")
        bar.add_class("prompt")
        bar.update(f" {message} [y/N]")

    def _cancel_status_prompt(self) -> None:
        if not self.status_prompt:
            return
        self.status_prompt = None
        self.clear_status()

    def action_status_prompt_y(self) -> None:
        if not self.status_prompt:
            return
        on_confirm = self.status_prompt["on_confirm"]
        self.status_prompt = None
        bar = self.query_one("#status_bar", Static)
        bar.remove_class("prompt")
        on_confirm()

    def action_status_prompt_n(self) -> None:
        self._cancel_status_prompt()

    def _status_prompt_blocks(self, action: str) -> bool:
        if not self.status_prompt:
            return False
        self.show_status(f"answer [y/N] or press n to cancel ({action})", timeout=2.0)
        return True

    def on_mount(self) -> None:
        payload = load_data(DATA_FILE)
        self.positions = [Position.from_dict(item) for item in payload.get("positions", [])]
        self.viewer_ticker = str(payload.get("viewer", {}).get("ticker", "AAPL")).strip().upper() or "AAPL"
        self.viewer_timeframe = str(payload.get("viewer", {}).get("timeframe", "1M")).upper()
        if self.viewer_timeframe not in TIMEFRAME_MAP:
            self.viewer_timeframe = "1M"

        ticker_input = self.query_one("#ticker_input", Input)
        ticker_input.value = self.viewer_ticker

        self._setup_positions_table()
        self._render_positions_table()
        self._render_allocation_view()
        self._update_timeframe_buttons()
        self._render_market_view()

        self.set_interval(15.0, self._auto_refresh)
        self._trigger_refresh(force=True, reason="startup")
        self.call_after_refresh(self._focus_positions_table)

    def _setup_positions_table(self) -> None:
        table = self.query_one("#positions_table", DataTable)
        if table.columns:
            return
        table.add_column("TICKER", width=9)
        table.add_column("CATEGORY", width=11)
        table.add_column("SECTOR", width=16)
        table.add_column("SHARES", width=10)
        table.add_column("COST BASIS", width=12)
        table.add_column("CURRENT PRICE", width=14)
        table.add_column("TOTAL VALUE", width=14)
        table.add_column("GAIN/LOSS ($)", width=14)
        table.add_column("GAIN/LOSS (%)", width=14)

    def _active_tab(self) -> str:
        tabs = self.query_one("#tabs", TabbedContent)
        return tabs.active

    def _set_tab(self, tab_id: str) -> None:
        self.query_one("#tabs", TabbedContent).active = tab_id

    def action_tab_positions(self) -> None:
        self._set_tab("positions")
        self.call_after_refresh(self._focus_positions_table)

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        if event.tab.id == "positions":
            self.call_after_refresh(self._focus_positions_table)

    def _focus_positions_table(self) -> None:
        if self._active_tab() != "positions" or not self.positions:
            return
        table = self.query_one("#positions_table", DataTable)
        row = max(0, min(self.selected_position_row, len(self.positions) - 1))
        table.move_cursor(row=row)
        table.focus()

    def action_tab_viewer(self) -> None:
        self._set_tab("viewer")

    def action_tab_allocation(self) -> None:
        self._set_tab("allocation")

    def action_refresh_now(self) -> None:
        if self._status_prompt_blocks("refresh"):
            return
        self._trigger_refresh(force=False, reason="manual")

    def _auto_refresh(self) -> None:
        self._trigger_refresh(force=True, reason="auto")

    def _trigger_refresh(self, force: bool, reason: str) -> None:
        now = time.monotonic()
        if not force and now - self.last_refresh_request < 1.5:
            self.show_status("refresh ignored (debounced)")
            return
        if self.refresh_in_progress:
            self.show_status("refresh already running...")
            return
        self.last_refresh_request = now
        self.run_worker(self._refresh_data(reason), group="refresh-data", exclusive=True)

    async def _refresh_data(self, reason: str) -> None:
        self.refresh_in_progress = True
        self.query_one("#positions_status", Static).update(f"Refreshing data ({reason})...")

        try:
            symbols = sorted({position.ticker for position in self.positions if position.ticker})
            if symbols:
                tasks = [asyncio.to_thread(last_close_price, symbol) for symbol in symbols]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for symbol, result in zip(symbols, results):
                    if isinstance(result, Exception):
                        self.price_cache[symbol] = None
                    else:
                        self.price_cache[symbol] = result

            snapshot = await asyncio.to_thread(market_snapshot, self.viewer_ticker, self.viewer_timeframe)
            self.viewer_snapshot = snapshot
            viewer_price = snapshot.get("current_price")
            if viewer_price is not None:
                self.price_cache[self.viewer_ticker] = safe_float(viewer_price, default=None)

            self._render_positions_table()
            self._render_allocation_view()
            self._render_market_view()

            self.last_refresh_label = datetime.now().strftime("%H:%M:%S")
            self._render_positions_table()
        except Exception as exc:
            self.show_status(f"refresh failed: {exc}", level="error")
        finally:
            self.refresh_in_progress = False

    def _render_positions_table(self) -> None:
        table = self.query_one("#positions_table", DataTable)
        had_focus = table.has_focus
        if table.cursor_row is not None and 0 <= table.cursor_row < len(self.positions):
            self.selected_position_row = table.cursor_row
        table.clear()
        self.row_to_position = []

        total_value = 0.0
        total_cost = 0.0

        for idx, position in enumerate(self.positions):
            price = self.price_cache.get(position.ticker)
            cost_total = position.shares * position.cost_basis
            total_cost += cost_total
            current_price_display = Text("N/A", style="dim")
            total_value_display = Text("N/A", style="dim")
            gain_display = Text("N/A", style="dim")
            gain_pct_display = Text("N/A", style="dim")

            if price is not None:
                position_value = position.shares * price
                gain_value = position_value - cost_total
                gain_pct = (gain_value / cost_total) * 100 if cost_total else 0.0

                total_value += position_value

                gain_style = "bold bright_green" if gain_value >= 0 else "bold bright_red"
                current_price_display = Text(format_money(price))
                total_value_display = Text(format_money(position_value))
                gain_display = Text(format_money(gain_value), style=gain_style)
                gain_pct_display = Text(format_percent(gain_pct), style=gain_style)
            else:
                total_value += cost_total

            category = normalize_category(position.category)
            category_style = CATEGORY_COLORS.get(category, "white")

            table.add_row(
                Text(position.ticker, style="bold"),
                Text(category, style=category_style),
                Text(position.sector),
                Text(f"{position.shares:,.4f}".rstrip("0").rstrip(".")),
                Text(format_money(position.cost_basis)),
                current_price_display,
                total_value_display,
                gain_display,
                gain_pct_display,
            )
            self.row_to_position.append(idx)

        if not self.positions:
            table.add_row(
                Text("-", style="dim"),
                Text("-", style="dim"),
                Text("No positions yet. Press 'a' to add one.", style="dim"),
                Text("-", style="dim"),
                Text("-", style="dim"),
                Text("-", style="dim"),
                Text("-", style="dim"),
                Text("-", style="dim"),
                Text("-", style="dim"),
            )

        overall_gain = total_value - total_cost
        overall_pct = (overall_gain / total_cost) * 100 if total_cost else 0.0
        gain_style = "bold bright_green" if overall_gain >= 0 else "bold bright_red"
        summary = Text.assemble(
            ("Portfolio Value: ", "bold"),
            (format_money(total_value), "bold"),
            ("   Invested: ", "bold"),
            (format_money(total_cost), "bold"),
            ("   P/L: ", "bold"),
            (f"{format_money(overall_gain)} ({format_percent(overall_pct)})", gain_style),
            ("   Last Refresh: ", "bold"),
            (f"{self.last_refresh_label} (15s auto)", "dim"),
        )
        self.query_one("#positions_status", Static).update(summary)

        if self.positions:
            row = max(0, min(self.selected_position_row, len(self.positions) - 1))
            self.selected_position_row = row
            table.move_cursor(row=row)
            if had_focus and self._active_tab() == "positions":
                table.focus()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id != "positions_table":
            return
        if event.cursor_row is not None and event.cursor_row < len(self.row_to_position):
            self.selected_position_row = event.cursor_row

    def _render_allocation_view(self) -> None:
        allocation_widget = self.query_one("#allocation_view", Static)
        if not self.positions:
            allocation_widget.update("No positions available for allocation analysis.")
            return

        value_by_category: dict[str, float] = {}
        value_by_sector: dict[str, float] = {}

        total_value = 0.0
        for position in self.positions:
            current_price = self.price_cache.get(position.ticker)
            if current_price is None:
                current_price = position.cost_basis
            position_value = position.shares * current_price
            total_value += position_value

            category = normalize_category(position.category)
            value_by_category[category] = value_by_category.get(category, 0.0) + position_value
            value_by_sector[position.sector] = value_by_sector.get(position.sector, 0.0) + position_value

        if total_value <= 0:
            allocation_widget.update("Portfolio value is zero.")
            return

        category_table = Table(title="By Category", box=None, show_header=True, pad_edge=False)
        category_table.add_column("CATEGORY", style="bold")
        category_table.add_column("VALUE", justify="right")
        category_table.add_column("%", justify="right")
        category_table.add_column("BAR")
        for category, value in sorted(value_by_category.items(), key=lambda item: item[1], reverse=True):
            percent = (value / total_value) * 100
            category_table.add_row(
                Text(category, style=CATEGORY_COLORS.get(category, "white")),
                format_money(value),
                format_percent(percent),
                alloc_bar(percent),
            )

        sector_table = Table(title="By Sector", box=None, show_header=True, pad_edge=False)
        sector_table.add_column("SECTOR", style="bold")
        sector_table.add_column("VALUE", justify="right")
        sector_table.add_column("%", justify="right")
        sector_table.add_column("BAR")
        for sector, value in sorted(value_by_sector.items(), key=lambda item: item[1], reverse=True):
            percent = (value / total_value) * 100
            sector_table.add_row(
                sector,
                format_money(value),
                format_percent(percent),
                alloc_bar(percent),
            )

        panels = Columns(
            [
                Panel(category_table, title="Category Allocation"),
                Panel(sector_table, title="Sector Allocation"),
            ],
            equal=True,
            expand=True,
        )
        allocation_widget.update(panels)

    def _render_market_view(self) -> None:
        summary_widget = self.query_one("#viewer_summary", Static)
        chart_widget = self.query_one("#viewer_chart", Static)
        snapshot = self.viewer_snapshot

        if not snapshot:
            summary_widget.update("Market viewer data unavailable. Press 'r' to refresh.")
            chart_widget.update("No chart data.")
            return

        current_price = snapshot.get("current_price")
        change_value = safe_float(snapshot.get("change_value"))
        change_pct = safe_float(snapshot.get("change_pct"))
        price_text = format_money(current_price) if current_price is not None else "N/A"
        change_style = "bold bright_green" if change_value >= 0 else "bold bright_red"
        change_text = Text(f"{change_value:+,.2f} ({change_pct:+,.2f}%)", style=change_style)

        details = Table.grid(padding=(0, 2))
        details.add_column(style="bold")
        details.add_column()
        details.add_row("Company", f"{snapshot.get('name', self.viewer_ticker)} ({self.viewer_ticker})")
        details.add_row("Price", price_text)
        details.add_row("Day Move", change_text)
        details.add_row("Sector", str(snapshot.get("sector", "N/A")))
        details.add_row("Market Cap", format_money(safe_float(snapshot.get("market_cap"))) if snapshot.get("market_cap") else "N/A")
        details.add_row("P/E", f"{safe_float(snapshot.get('pe')):,.2f}" if snapshot.get("pe") else "N/A")
        low = snapshot.get("week52_low")
        high = snapshot.get("week52_high")
        if low is not None and high is not None:
            details.add_row("52W Range", f"{format_money(safe_float(low))} - {format_money(safe_float(high))}")
        else:
            details.add_row("52W Range", "N/A")
        details.add_row("Volume", f"{int(safe_float(snapshot.get('volume'))):,}" if snapshot.get("volume") else "N/A")
        summary_widget.update(Panel(details, title=f"Market Viewer - {self.viewer_ticker} ({self.viewer_timeframe})"))

        width = max(50, chart_widget.size.width - 6)
        height = max(12, chart_widget.size.height - 2)
        chart_renderable = build_chart(
            points=snapshot.get("chart_points", []),
            timeframe=self.viewer_timeframe,
            symbol=self.viewer_ticker,
            width=width,
            height=height,
            prev_close=safe_float(snapshot.get("prev_close")) if self.viewer_timeframe == "1D" else None,
        )
        chart_widget.update(chart_renderable)

    def _selected_position_index(self) -> Optional[int]:
        if not self.positions:
            return None
        table = self.query_one("#positions_table", DataTable)
        cursor_row = table.cursor_row
        if cursor_row is None:
            cursor_row = self.selected_position_row
        if cursor_row < 0 or cursor_row >= len(self.row_to_position):
            return None
        return self.row_to_position[cursor_row]

    def action_add_position(self) -> None:
        if self._status_prompt_blocks("add"):
            return
        if self._active_tab() != "positions":
            return
        self.push_screen(PositionEditorScreen(), self._handle_add_result)

    def action_edit_position(self) -> None:
        if self._status_prompt_blocks("edit"):
            return
        if self._active_tab() != "positions":
            return
        position_index = self._selected_position_index()
        if position_index is None:
            self.show_status("select a position to edit first", level="error")
            return
        self.push_screen(
            PositionEditorScreen(self.positions[position_index]),
            lambda payload: self._handle_edit_result(position_index, payload),
        )

    def action_delete_position(self) -> None:
        if self._active_tab() != "positions":
            return
        if self.status_prompt:
            self.show_status("answer [y/N] or press n to cancel", timeout=2.0)
            return
        position_index = self._selected_position_index()
        if position_index is None:
            self.show_status("select a position to delete first", level="error")
            return
        ticker = self.positions[position_index].ticker
        self.begin_status_prompt(
            f"delete {ticker}?",
            on_confirm=lambda idx=position_index: self._execute_delete(idx),
        )

    def _handle_add_result(self, payload: Optional[dict[str, str]]) -> None:
        if payload is None:
            return
        self.run_worker(self._upsert_position(payload, edit_index=None), group="position-upsert", exclusive=True)

    def _handle_edit_result(self, index: int, payload: Optional[dict[str, str]]) -> None:
        if payload is None:
            return
        self.run_worker(self._upsert_position(payload, edit_index=index), group="position-upsert", exclusive=True)

    def _execute_delete(self, index: int) -> None:
        if index < 0 or index >= len(self.positions):
            self.show_status("selection is no longer valid", level="error")
            return
        ticker = self.positions[index].ticker
        self.positions.pop(index)
        if self.positions:
            self.selected_position_row = min(index, len(self.positions) - 1)
        else:
            self.selected_position_row = 0
        self._save_portfolio()
        self._render_positions_table()
        if self.positions:
            self.call_after_refresh(self._focus_positions_table)
        self._render_allocation_view()
        self.show_status(f"removed {ticker}", level="success")

    async def _upsert_position(self, payload: dict[str, str], edit_index: Optional[int]) -> None:
        ticker = payload.get("ticker", "").strip().upper()
        category = normalize_category(payload.get("category", "Neutral"))
        sector = payload.get("sector", "").strip()
        shares_raw = payload.get("shares", "").strip()
        cost_basis_raw = payload.get("cost_basis", "").strip()

        if not ticker:
            self.show_status("ticker is required", level="error")
            return
        if not shares_raw:
            self.show_status("shares are required", level="error")
            return
        if not cost_basis_raw:
            self.show_status("cost basis is required", level="error")
            return

        try:
            shares = float(shares_raw)
            cost_basis = float(cost_basis_raw)
        except ValueError:
            self.show_status("shares and cost basis must be numbers", level="error")
            return

        if shares <= 0:
            self.show_status("shares must be greater than zero", level="error")
            return
        if cost_basis < 0:
            self.show_status("cost basis must be zero or greater", level="error")
            return

        valid, sector_from_api, error_message = await asyncio.to_thread(validate_ticker, ticker)
        if not valid:
            self.show_status(error_message, level="error")
            return

        if not sector:
            sector = sector_from_api or "Unknown"

        position = Position(
            ticker=ticker,
            category=category,
            sector=sector,
            shares=shares,
            cost_basis=cost_basis,
        )
        if edit_index is None:
            self.positions.append(position)
            self.show_status(f"added {ticker}", level="success")
        else:
            if 0 <= edit_index < len(self.positions):
                self.positions[edit_index] = position
                self.show_status(f"updated {ticker}", level="success")
            else:
                self.show_status("selection is no longer valid", level="error")
                return

        self.positions.sort(key=lambda item: item.ticker)
        self._save_portfolio()
        self._trigger_refresh(force=True, reason="position change")

    def action_save_portfolio(self) -> None:
        if self._status_prompt_blocks("save"):
            return
        if self._active_tab() != "positions":
            return
        self._save_portfolio()
        self.show_status("portfolio saved", level="success")

    def _save_portfolio(self) -> None:
        try:
            save_data(DATA_FILE, self.positions, self.viewer_ticker, self.viewer_timeframe)
        except OSError as exc:
            self.show_status(f"save failed: {exc}", level="error")

    def action_focus_ticker(self) -> None:
        if self._active_tab() != "viewer":
            return
        self.query_one("#ticker_input", Input).focus()

    def action_blur_input(self) -> None:
        if self.status_prompt:
            self._cancel_status_prompt()
            return
        focused = self.focused
        if isinstance(focused, Input):
            self.set_focus(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "ticker_input":
            return
        if self._status_prompt_blocks("ticker"):
            return
        symbol = event.value.strip().upper()
        if not symbol:
            self.show_status("enter a ticker symbol first", level="error")
            return
        self.viewer_ticker = symbol
        self._save_portfolio()
        self._trigger_refresh(force=True, reason="ticker update")

    def _viewer_timeframe_keys_active(self) -> bool:
        if self._active_tab() != "viewer":
            return False
        return not isinstance(self.focused, Input)

    def _update_timeframe_buttons(self) -> None:
        for timeframe in TIMEFRAME_OPTIONS:
            button = self.query_one(f"#{timeframe_button_id(timeframe)}", Button)
            button.variant = "primary" if timeframe == self.viewer_timeframe else "default"

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if not button_id or not button_id.startswith("tf_btn_"):
            return
        timeframe = button_id.removeprefix("tf_btn_").upper()
        if timeframe in TIMEFRAME_MAP:
            self._set_timeframe(timeframe)

    def _set_timeframe(self, timeframe: str, *, from_keyboard: bool = False) -> None:
        if self._active_tab() != "viewer":
            return
        if from_keyboard and not self._viewer_timeframe_keys_active():
            return
        if timeframe not in TIMEFRAME_MAP:
            return
        if timeframe == self.viewer_timeframe:
            return
        self.viewer_timeframe = timeframe
        self._update_timeframe_buttons()
        self._save_portfolio()
        self.show_status(f"timeframe: {timeframe}")
        self._trigger_refresh(force=True, reason=f"timeframe {timeframe}")

    def action_tf_prev(self) -> None:
        if not self._viewer_timeframe_keys_active():
            return
        index = TIMEFRAME_OPTIONS.index(self.viewer_timeframe)
        self._set_timeframe(TIMEFRAME_OPTIONS[(index - 1) % len(TIMEFRAME_OPTIONS)])

    def action_tf_next(self) -> None:
        if not self._viewer_timeframe_keys_active():
            return
        index = TIMEFRAME_OPTIONS.index(self.viewer_timeframe)
        self._set_timeframe(TIMEFRAME_OPTIONS[(index + 1) % len(TIMEFRAME_OPTIONS)])

    def action_tf_1d(self) -> None:
        self._set_timeframe("1D", from_keyboard=True)

    def action_tf_5d(self) -> None:
        self._set_timeframe("5D", from_keyboard=True)

    def action_tf_1m(self) -> None:
        self._set_timeframe("1M", from_keyboard=True)

    def action_tf_3m(self) -> None:
        self._set_timeframe("3M", from_keyboard=True)

    def action_tf_6m(self) -> None:
        self._set_timeframe("6M", from_keyboard=True)

    def action_tf_1y(self) -> None:
        self._set_timeframe("1Y", from_keyboard=True)

    def action_tf_5y(self) -> None:
        self._set_timeframe("5Y", from_keyboard=True)

    def action_tf_all(self) -> None:
        self._set_timeframe("ALL", from_keyboard=True)


if __name__ == "__main__":
    PortfolioTrackerApp().run()
