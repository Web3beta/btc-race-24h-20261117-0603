"""Dip-Recovery Hunter: a long-only cbBTC/USDC bot for agent-fun/bars-replay/v1.

The harness speaks first with a hello, then sends one batch per bar, and the bot
answers every batch with exactly one reply carrying its orders for that bar. Each
batch holds the new bar (open/high/low/close), the fills of orders decided on the
previous bar, and the account. Orders decided on a bar fill at the next bar's open.

Strategy (from the design spec, implemented here in my own words):
  * Buy dips: the close sits at least 1.2% below the trailing 240-bar high and
    has bounced at least 0.15% off the trailing 15-bar low, but only while the
    12-hour trend is not down more than 1% (trend filter).
  * If the last completed trade lost money, the next dip must be deeper (2.5%)
    before buying again.
  * Exits: take profit +1.2%, stop loss -1.0%, or a 4-hour (240-bar) time exit.
  * Backstops so the bot always trades at least once: a strict EMA trend arm
    after bar 600, and a small rescue trade after bar 1200.
  * Guards: 30-bar cooldown after a sell, at most 4 trades, no new buys once
    equity has fallen 3% below the $1,000 start. Long only, no leverage, no
    shorts, no randomness: fully deterministic, same input gives same output.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

SYMBOL = "CBBTC"

# --- Strategy parameters (validated values from the design spec) ---
DIP = 0.012            # dip vs trailing 240-bar high
DIP_AFTER_LOSS = 0.025  # deeper dip required after a losing trade
BOUNCE = 0.0015        # bounce above trailing 15-bar low
HI_WINDOW = 240        # trailing-high window, in bars
LO_WINDOW = 15         # bounce window, in bars
TP = 0.012             # take-profit above entry close
SL = 0.010             # stop-loss below entry close
MAX_HOLD = 240         # max hold, in bars (4 hours)
COOLDOWN = 30          # bars to wait after a sell
TREND_WINDOW = 720     # trend-filter lookback, in bars (12 hours)
TREND_MIN = -0.010     # block dip-buys if the 12h return is below this
TREND_ARM_BAR = 600    # earliest bar for the trend arm (10 hours)
EMA_FAST_N = 60        # fast EMA length, in bars
EMA_SLOW_N = 240       # slow EMA length, in bars
TREND_MIN_360 = 0.005  # trend arm needs the 360-bar return above this
RESCUE_BAR = 1200      # earliest bar for the rescue trade (20 hours)
RESCUE_TP = 0.006      # rescue take-profit
RESCUE_SL = 0.006      # rescue stop-loss
RESCUE_HOLD = 60       # rescue max hold, in bars
LOSS_BRAKE_EQUITY = 970_000_000  # stop new buys if equity falls under $970
MAX_TRADES = 4         # hard cap on completed round trips
MAX_SLIPPAGE_BPS = 150


class Hunter:
    """Keeps the bot's state across bars; one instance serves the whole run."""

    def __init__(self):
        self.closes = []          # every close seen, ints, price-scale-5
        self.entry_close = None   # close at the bar the current buy was decided
        self.entry_bar = None     # bar index of that buy decision
        self.entry_fill_price = None  # fill price of the current position
        self.buy_fee = 0          # fee paid on the current position's buy fill
        self.last_sell_bar = None
        self.last_trade_pnl = None  # quote units; negative means the trade lost
        self.trades = 0           # completed round trips
        self.trend_arm_used = False
        self.rescue_mode = False
        self.ema_fast = None
        self.ema_slow = None
        self.prev_fast = None
        self.prev_slow = None

    # -- helpers ---------------------------------------------------------
    def _value(self, state, qty, price):
        """What qty is worth at price, in quote units (uses the shim's math)."""
        return state.value(SYMBOL, qty, price)

    def _record_fills(self, state, batch):
        """Fold this batch's fills into the trade book.

        A buy fill fixes the entry price and fee of the position just opened.
        A sell fill (ours, or the harness's end-of-window flatten) closes a
        round trip: count it and record its true PnL for the re-entry rule.
        """
        for fill in batch.get("fills") or []:
            if fill.get("symbol") != SYMBOL or fill.get("status") != "filled":
                continue
            price = int(fill["price"])
            qty = int(fill["qty"])
            fee = int(fill["fee_quote_units"])
            if fill.get("side") == "buy":
                self.entry_fill_price = price
                self.buy_fee = fee
            elif fill.get("side") == "sell":
                if self.entry_fill_price is not None:
                    entry_price = self.entry_fill_price
                    entry_fee = self.buy_fee
                elif self.entry_close is not None:
                    entry_price = self.entry_close
                    entry_fee = 0
                else:
                    entry_price = None
                if entry_price is not None:
                    proceeds = self._value(state, qty, price) - fee
                    cost = self._value(state, qty, entry_price) + entry_fee
                    self.last_trade_pnl = proceeds - cost
                self.trades += 1
                self.entry_fill_price = None
                self.buy_fee = 0

    def _update_indicators(self, close):
        """Append the new close and refresh the two EMAs, keeping the previous
        values so the next step can spot a crossover."""
        self.closes.append(close)
        if self.ema_fast is None:
            self.ema_fast = float(close)
            self.ema_slow = float(close)
            self.prev_fast = None
            self.prev_slow = None
            return
        self.prev_fast = self.ema_fast
        self.prev_slow = self.ema_slow
        k_fast = 2.0 / (EMA_FAST_N + 1)
        k_slow = 2.0 / (EMA_SLOW_N + 1)
        self.ema_fast = close * k_fast + self.ema_fast * (1.0 - k_fast)
        self.ema_slow = close * k_slow + self.ema_slow * (1.0 - k_slow)

    def _dip_fires(self, close):
        """True when the dip signal fires and the trend filter lets it through."""
        window = self.closes[-HI_WINDOW:]
        hi = max(window)
        dip = (hi - close) / hi if hi > 0 else 0.0
        lo = min(self.closes[-LO_WINDOW:])
        bounce = (close - lo) / lo if lo > 0 else 0.0
        threshold = DIP_AFTER_LOSS if (self.last_trade_pnl is not None
                                       and self.last_trade_pnl < 0) else DIP
        if dip < threshold or bounce < BOUNCE:
            return False
        n = len(self.closes)
        ref = self.closes[n - 1 - TREND_WINDOW] if n > TREND_WINDOW else self.closes[0]
        if (close - ref) / ref < TREND_MIN:
            return False  # 12-hour trend is down: do not catch the falling knife
        return True

    def _trend_arm_fires(self, close, bar_index):
        """Strict momentum only: EMA-60 crosses above EMA-240, the close sits
        above the fast EMA, and the 360-bar return is positive."""
        if bar_index < TREND_ARM_BAR or self.trend_arm_used:
            return False
        if self.prev_fast is None or self.prev_slow is None:
            return False
        if not (self.prev_fast <= self.prev_slow and self.ema_fast > self.ema_slow):
            return False
        if not (close > self.ema_fast):
            return False
        n = len(self.closes)
        ref360 = self.closes[n - 361] if n > 360 else self.closes[0]
        if (close - ref360) / ref360 <= TREND_MIN_360:
            return False
        return True

    def _rescue_fires(self, close, bar_index):
        """Any uptick once we are deep into the window with no trade yet."""
        if bar_index < RESCUE_BAR or len(self.closes) < 2:
            return False
        return close >= self.closes[-2]

    # -- main callback ---------------------------------------------------
    def on_batch(self, state, batch):
        """Decide this bar's orders. Called once per batch by the shim."""
        close = state.close(SYMBOL)
        if close is None:
            return []
        self._record_fills(state, batch)
        self._update_indicators(close)
        bar_index = len(self.closes) - 1

        if state.warming_up:
            # Warm-up bars exist only to build history; orders on them are rejected.
            return []

        holding = state.positions.get(SYMBOL)
        qty = holding.qty if holding is not None else 0

        if qty > 0:
            return self._maybe_sell(state, close, bar_index, qty)
        return self._maybe_buy(state, close, bar_index)

    def _maybe_sell(self, state, close, bar_index, qty):
        """Exit the open position when TP, SL or the time exit hits."""
        if self.entry_close is None:
            return []
        if self.rescue_mode:
            tp, sl, hold = RESCUE_TP, RESCUE_SL, RESCUE_HOLD
        else:
            tp, sl, hold = TP, SL, MAX_HOLD
        chg = (close - self.entry_close) / self.entry_close
        entry_bar = self.entry_bar if self.entry_bar is not None else bar_index
        if chg >= tp or chg <= -sl or (bar_index - entry_bar) >= hold:
            self.last_sell_bar = bar_index
            self.rescue_mode = False
            return [{"side": "sell", "symbol": SYMBOL, "qty": qty,
                     "max_slippage_bps": MAX_SLIPPAGE_BPS}]
        return []

    def _maybe_buy(self, state, close, bar_index):
        """Buy when flat and a signal fires, unless a guard blocks it."""
        if self.trades >= MAX_TRADES:
            return []
        if self.last_sell_bar is not None and (bar_index - self.last_sell_bar) < COOLDOWN:
            return []
        if state.equity < LOSS_BRAKE_EQUITY:
            return []

        # Evaluate in the spec's order: dip first, then the trend arm, then
        # the rescue. The first signal that fires decides the trade's kind.
        kind = None
        if self._dip_fires(close):
            kind = "dip"
        elif self.trades == 0 and self._trend_arm_fires(close, bar_index):
            kind = "trend"
        elif self.trades == 0 and self._rescue_fires(close, bar_index):
            kind = "rescue"
        else:
            return []

        cash = state.cash
        if cash <= 0:
            return []
        self.entry_close = close
        self.entry_bar = bar_index
        if kind == "trend":
            self.trend_arm_used = True
        if kind == "rescue":
            self.rescue_mode = True
        return [{"side": "buy", "symbol": SYMBOL,
                 "notional_quote_units": cash,
                 "max_slippage_bps": MAX_SLIPPAGE_BPS}]


def main():
    hunter = Hunter()
    makefun_bars.run(hunter.on_batch)


if __name__ == "__main__":
    main()
