"""
Stock Portfolio Tracker CLI (full-screen interactive TUI).

Install:
    pip install textual yfinance plotext

Run:
    python portfolio_tracker.py

Key bindings:
    1 / 2 / 3     Switch tabs (Positions / Market Viewer / Allocation)
    r             Refresh data now
    q             Quit
    a             Add position (Positions tab)
    e             Edit selected position (Positions tab)
    d             Delete selected position (Positions tab)
    s             Save holdings JSON (Positions tab)
    /             Focus ticker input (Market Viewer tab)
    ctrl+1..ctrl+8  Market chart timeframes (1D,5D,1M,3M,6M,1Y,5Y,ALL)
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import yfinance as yf
from rich.columns import Columns
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, Static, TabbedContent, TabPane

DATA_FILE = Path(__file__).with_name("portfolio.json")
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


def build_chart(points: list[tuple[datetime, float]], timeframe: str, symbol: str, width: int, height: int) -> str:
    if not points:
        return "No chart data available for the selected timeframe."

    try:
        import plotext as plt
    except Exception:
        return "Install 'plotext' to render the terminal chart."

    prices = [point[1] for point in points]
    has_intraday = any(point[0].hour != 0 or point[0].minute != 0 for point in points)
    date_form = "d/m/Y H:M" if has_intraday else "d/m/Y"
    dates = [point[0].strftime("%d/%m/%Y %H:%M") if has_intraday else point[0].strftime("%d/%m/%Y") for point in points]
    width = max(45, width)
    height = max(12, height)

    chart = ""
    try:
        plt.clear_figure()
        plt.theme("pro")
        plt.date_form(date_form)
        plt.plotsize(width, height)
        plt.plot(dates, prices, marker="dot", color="cyan")
        plt.title(f"{symbol} ({timeframe})")
        plt.xlabel("Date")
        plt.ylabel("Price")

        built = plt.build()
        if isinstance(built, str):
            chart = built
        elif built is not None:
            chart = str(built)
    except Exception:
        # Fallback to numeric x-axis if string date parsing fails in plotext.
        try:
            plt.clear_figure()
            plt.theme("pro")
            plt.plotsize(width, height)
            plt.plot(list(range(len(prices))), prices, marker="dot", color="cyan")
            plt.title(f"{symbol} ({timeframe})")
            plt.xlabel(f"Points ({dates[0]} -> {dates[-1]})")
            plt.ylabel("Price")
            built = plt.build()
            if isinstance(built, str):
                chart = built
            elif built is not None:
                chart = str(built)
        except Exception:
            chart = ""

    if not chart.strip():
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            plt.show()
        chart = buffer.getvalue()

    plt.clear_figure()
    return chart.strip() or "Chart could not be rendered."


def alloc_bar(percent: float, width: int = 24) -> str:
    percent = max(0.0, min(100.0, percent))
    filled = int(round((percent / 100.0) * width))
    return ("#" * filled) + ("-" * (width - filled))


class PositionEditorScreen(ModalScreen[Optional[dict[str, str]]]):
    BINDINGS = [Binding("escape", "cancel", "Cancel"), Binding("enter", "submit", "Submit")]

    CSS = """
    PositionEditorScreen {
        align: center middle;
    }
    #position_form {
        width: 70;
        height: auto;
        padding: 1 2;
        border: solid $accent;
        background: $surface;
    }
    #position_form Input {
        margin: 1 0;
    }
    #position_buttons {
        width: 100%;
        height: auto;
        content-align: right middle;
        margin-top: 1;
    }
    """

    def __init__(self, initial: Optional[Position] = None) -> None:
        super().__init__()
        self.initial = initial

    def compose(self) -> ComposeResult:
        title = "Edit Position" if self.initial else "Add Position"
        ticker = self.initial.ticker if self.initial else ""
        category = self.initial.category if self.initial else "Neutral"
        sector = self.initial.sector if self.initial else ""
        shares = f"{self.initial.shares:.6f}".rstrip("0").rstrip(".") if self.initial else ""
        cost_basis = f"{self.initial.cost_basis:.4f}".rstrip("0").rstrip(".") if self.initial else ""

        with Container(id="position_form"):
            yield Static(f"[b]{title}[/b]")
            yield Static("Category must be Offensive, Neutral, or Defensive.")
            yield Input(value=ticker, id="pos_ticker", placeholder="Ticker (e.g. AAPL)")
            yield Input(value=category, id="pos_category", placeholder="Category")
            yield Input(value=sector, id="pos_sector", placeholder="Sector (optional)")
            yield Input(value=shares, id="pos_shares", placeholder="Shares (e.g. 10.5)")
            yield Input(value=cost_basis, id="pos_cost_basis", placeholder="Cost Basis per share (e.g. 180.25)")
            with Horizontal(id="position_buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Save", variant="primary", id="save")

    def on_mount(self) -> None:
        self.query_one("#pos_ticker", Input).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_submit(self) -> None:
        self._submit_form()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save":
            self._submit_form()
        else:
            self.dismiss(None)

    def _submit_form(self) -> None:
        payload = {
            "ticker": self.query_one("#pos_ticker", Input).value.strip().upper(),
            "category": self.query_one("#pos_category", Input).value.strip(),
            "sector": self.query_one("#pos_sector", Input).value.strip(),
            "shares": self.query_one("#pos_shares", Input).value.strip(),
            "cost_basis": self.query_one("#pos_cost_basis", Input).value.strip(),
        }
        self.dismiss(payload)


class ConfirmDeleteScreen(ModalScreen[bool]):
    BINDINGS = [Binding("y", "confirm", "Yes"), Binding("n", "cancel", "No"), Binding("escape", "cancel", "No")]

    CSS = """
    ConfirmDeleteScreen {
        align: center middle;
    }
    #confirm_box {
        width: 60;
        height: auto;
        padding: 1 2;
        border: solid $error;
        background: $surface;
    }
    #confirm_buttons {
        width: 100%;
        content-align: right middle;
        margin-top: 1;
    }
    """

    def __init__(self, ticker: str) -> None:
        super().__init__()
        self.ticker = ticker

    def compose(self) -> ComposeResult:
        with Container(id="confirm_box"):
            yield Static(f"Delete position [b]{self.ticker}[/b]?")
            yield Static("Press Y to confirm, N to cancel.")
            with Horizontal(id="confirm_buttons"):
                yield Button("No", id="cancel")
                yield Button("Yes", id="confirm", variant="error")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")


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
        Binding("s", "save_portfolio", "Save"),
        Binding("/", "focus_ticker", "Ticker"),
        Binding("ctrl+1", "tf_1d", "TF:1D", show=False),
        Binding("ctrl+2", "tf_5d", "TF:5D", show=False),
        Binding("ctrl+3", "tf_1m", "TF:1M", show=False),
        Binding("ctrl+4", "tf_3m", "TF:3M", show=False),
        Binding("ctrl+5", "tf_6m", "TF:6M", show=False),
        Binding("ctrl+6", "tf_1y", "TF:1Y", show=False),
        Binding("ctrl+7", "tf_5y", "TF:5Y", show=False),
        Binding("ctrl+8", "tf_all", "TF:ALL", show=False),
    ]

    CSS = """
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
    }
    #allocation_view {
        height: 1fr;
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
        self.last_refresh_request = 0.0
        self.refresh_in_progress = False
        self.last_refresh_label = "Never"

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
                    yield Static("", id="viewer_chart")
            with TabPane("Allocation", id="allocation"):
                yield Static("", id="allocation_view")
        yield Footer()

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
        self._render_market_view()

        self.set_interval(15.0, self._auto_refresh)
        self._trigger_refresh(force=True, reason="startup")

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

    def action_tab_viewer(self) -> None:
        self._set_tab("viewer")

    def action_tab_allocation(self) -> None:
        self._set_tab("allocation")

    def action_refresh_now(self) -> None:
        self._trigger_refresh(force=False, reason="manual")

    def _auto_refresh(self) -> None:
        self._trigger_refresh(force=True, reason="auto")

    def _trigger_refresh(self, force: bool, reason: str) -> None:
        now = time.monotonic()
        if not force and now - self.last_refresh_request < 1.5:
            self.notify("Refresh ignored (debounced).")
            return
        if self.refresh_in_progress:
            self.notify("Refresh already running...")
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
            self.notify(f"Refresh failed: {exc}", severity="error")
        finally:
            self.refresh_in_progress = False

    def _render_positions_table(self) -> None:
        table = self.query_one("#positions_table", DataTable)
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
        details.add_row(
            "Timeframes",
            "ctrl+1 1D | ctrl+2 5D | ctrl+3 1M | ctrl+4 3M | ctrl+5 6M | ctrl+6 1Y | ctrl+7 5Y | ctrl+8 ALL",
        )
        summary_widget.update(Panel(details, title=f"Market Viewer - {self.viewer_ticker} ({self.viewer_timeframe})"))

        width = max(50, chart_widget.size.width - 4)
        height = max(14, chart_widget.size.height - 4)
        chart_text = build_chart(
            points=snapshot.get("chart_points", []),
            timeframe=self.viewer_timeframe,
            symbol=self.viewer_ticker,
            width=width,
            height=height,
        )
        chart_widget.update(Panel(Text(chart_text), title=f"{self.viewer_ticker} Price Chart"))

    def _selected_position_index(self) -> Optional[int]:
        table = self.query_one("#positions_table", DataTable)
        cursor_row = table.cursor_row
        if cursor_row is None:
            return None
        if cursor_row < 0 or cursor_row >= len(self.row_to_position):
            return None
        return self.row_to_position[cursor_row]

    def action_add_position(self) -> None:
        if self._active_tab() != "positions":
            return
        self.push_screen(PositionEditorScreen(), self._handle_add_result)

    def action_edit_position(self) -> None:
        if self._active_tab() != "positions":
            return
        position_index = self._selected_position_index()
        if position_index is None:
            self.notify("Select a position to edit first.")
            return
        self.push_screen(
            PositionEditorScreen(self.positions[position_index]),
            lambda payload: self._handle_edit_result(position_index, payload),
        )

    def action_delete_position(self) -> None:
        if self._active_tab() != "positions":
            return
        position_index = self._selected_position_index()
        if position_index is None:
            self.notify("Select a position to delete first.")
            return
        ticker = self.positions[position_index].ticker
        self.push_screen(ConfirmDeleteScreen(ticker), lambda confirmed: self._handle_delete_result(position_index, confirmed))

    def _handle_add_result(self, payload: Optional[dict[str, str]]) -> None:
        if payload is None:
            return
        self.run_worker(self._upsert_position(payload, edit_index=None), group="position-upsert", exclusive=True)

    def _handle_edit_result(self, index: int, payload: Optional[dict[str, str]]) -> None:
        if payload is None:
            return
        self.run_worker(self._upsert_position(payload, edit_index=index), group="position-upsert", exclusive=True)

    def _handle_delete_result(self, index: int, confirmed: bool) -> None:
        if not confirmed:
            return
        if index < 0 or index >= len(self.positions):
            return
        ticker = self.positions[index].ticker
        self.positions.pop(index)
        self._save_portfolio()
        self._render_positions_table()
        self._render_allocation_view()
        self.notify(f"Removed {ticker}")

    async def _upsert_position(self, payload: dict[str, str], edit_index: Optional[int]) -> None:
        ticker = payload.get("ticker", "").strip().upper()
        category = normalize_category(payload.get("category", "Neutral"))
        sector = payload.get("sector", "").strip()
        shares_raw = payload.get("shares", "").strip()
        cost_basis_raw = payload.get("cost_basis", "").strip()

        if not ticker:
            self.notify("Ticker is required.", severity="error")
            return
        if not shares_raw:
            self.notify("Shares are required.", severity="error")
            return
        if not cost_basis_raw:
            self.notify("Cost basis is required.", severity="error")
            return

        try:
            shares = float(shares_raw)
            cost_basis = float(cost_basis_raw)
        except ValueError:
            self.notify("Shares and cost basis must be numbers.", severity="error")
            return

        if shares <= 0:
            self.notify("Shares must be greater than zero.", severity="error")
            return
        if cost_basis < 0:
            self.notify("Cost basis must be zero or greater.", severity="error")
            return

        valid, sector_from_api, error_message = await asyncio.to_thread(validate_ticker, ticker)
        if not valid:
            self.notify(error_message, severity="error")
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
            self.notify(f"Added {ticker}")
        else:
            if 0 <= edit_index < len(self.positions):
                self.positions[edit_index] = position
                self.notify(f"Updated {ticker}")
            else:
                self.notify("Selection is no longer valid.", severity="error")
                return

        self.positions.sort(key=lambda item: item.ticker)
        self._save_portfolio()
        self._trigger_refresh(force=True, reason="position change")

    def action_save_portfolio(self) -> None:
        if self._active_tab() != "positions":
            return
        self._save_portfolio()
        self.notify("Portfolio saved.")

    def _save_portfolio(self) -> None:
        try:
            save_data(DATA_FILE, self.positions, self.viewer_ticker, self.viewer_timeframe)
        except OSError as exc:
            self.notify(f"Save failed: {exc}", severity="error")

    def action_focus_ticker(self) -> None:
        if self._active_tab() != "viewer":
            return
        self.query_one("#ticker_input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "ticker_input":
            return
        symbol = event.value.strip().upper()
        if not symbol:
            self.notify("Enter a ticker symbol first.")
            return
        self.viewer_ticker = symbol
        self._save_portfolio()
        self._trigger_refresh(force=True, reason="ticker update")

    def _set_timeframe(self, timeframe: str) -> None:
        self.viewer_timeframe = timeframe
        self._save_portfolio()
        self._trigger_refresh(force=True, reason=f"timeframe {timeframe}")

    def action_tf_1d(self) -> None:
        self._set_timeframe("1D")

    def action_tf_5d(self) -> None:
        self._set_timeframe("5D")

    def action_tf_1m(self) -> None:
        self._set_timeframe("1M")

    def action_tf_3m(self) -> None:
        self._set_timeframe("3M")

    def action_tf_6m(self) -> None:
        self._set_timeframe("6M")

    def action_tf_1y(self) -> None:
        self._set_timeframe("1Y")

    def action_tf_5y(self) -> None:
        self._set_timeframe("5Y")

    def action_tf_all(self) -> None:
        self._set_timeframe("ALL")


if __name__ == "__main__":
    PortfolioTrackerApp().run()
