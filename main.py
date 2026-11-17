"""Dip-Recovery Hunter: long-only cbBTC/USDC bot for agent-fun/bars-replay/v1.

The harness sends hello first, then one batch per bar, and this bot answers
every batch with exactly one reply holding its orders for that bar. Each batch
carries the new bar (open/high/low/close), the fills of orders decided on the
previous bar, and the account. An order decided on a bar fills at the next
bar's open.

How it trades (written from the design spec, in my own words):
  * Dip buys: the close is under the trailing 240-bar high (0.9% normally,
    2.5% after a losing trade) and has bounced off the trailing 15-bar low
    (0.18%). The 12-hour return decides the SIZE, never a veto: full size
    while the 12h return is above -2.5%, half size below it, with a stronger
    bounce asked for in the down bands. At most 2 filled buys per trade cycle
    in the down bands, so a steady bleed cannot drain the account; the count
    resets whenever a round trip fully closes, and a rejected buy never uses
    up a slot (only filled buys count).
  * Slide guard: when the price fell 0.5%+ over the last 2 hours the market
    is sliding, not dipping, so the dip must be 2.0% deep with a 0.4% bounce;
    past a 1% slide in 2h it must be 2.5% deep with a 0.5% bounce. The 12h
    return is still near zero early on a down day and cannot see the slide
    yet, so this guard is what stops the bot bleeding there.
  * Exits use a trailing stop, not a fixed take-profit: a first stop 1.0%
    under entry; once the trade is up 0.8% the stop ratchets to entry +0.32%
    and then trails 0.3% under the highest close since entry (or the 15-bar
    low, whichever is tighter). Half the position is banked at +1.2% and the
    rest rides. Max hold is 12 hours, with an early exit on a close under the
    fast EMA after +0.5%.
  * Backstops so the bot always trades at least once: a strict EMA trend arm
    (EMA-60 crossing above EMA-240) that re-arms after every closed trade,
    and a rescue trade after bar 1380 if nothing has traded yet.
  * Guards: 15-bar cooldown after a sell, at most 4 round trips, no new buys
    once equity has fallen 15% under the $1,000 start, no buy before bar 60,
    no orders on the last bar (1439) on EITHER the buy or the sell path.
    Long only, no leverage, no shorts, no randomness: fully deterministic,
    same input gives same output.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

SYMBOL = "CBBTC"

# --- Strategy parameters (design spec, tuned on the dev windows) ---
DIP = 0.009             # dip vs trailing 240-bar high
DIP_AFTER_LOSS = 0.025  # deeper dip required after a losing trade
DIP_WASHOUT = 0.020     # dip required when the 12h return is below -5%
BOUNCE = 0.0018         # bounce above trailing 15-bar low
BOUNCE_DOWN = 0.0035    # bounce required in the down bands
BOUNCE_SLIDE = 0.004    # bounce required when the 2h return is below -0.5%
BOUNCE_SLIDE_DEEP = 0.005  # bounce required when the 2h return is below -1%
DIP_SLIDE = 0.020       # dip required when the 2h return is below -0.5%
DIP_SLIDE_DEEP = 0.025  # dip required when the 2h return is below -1%
HI_WINDOW = 240         # trailing-high window, in bars
LO_WINDOW = 15          # bounce window, in bars
SLIDE_WINDOW = 120      # short-term slide lookback, in bars (2 hours)
SLIDE_MID = -0.005      # 2h return below this: sliding market
SLIDE_DEEP = -0.010     # 2h return below this: deep slide
SL_INIT = 0.010         # initial stop-loss below entry close
BREAKEVEN_AT = 0.008    # move the stop up once unrealized reaches this
BREAKEVEN_STOP = 0.0032  # stop level after the breakeven ratchet (entry + 0.32%)
TRAIL = 0.003           # trailing stop below peak since entry (or 15-bar low)
PARTIAL_AT = 0.012      # take 50% off at this unrealized gain
MAX_HOLD = 720          # max hold, in bars (12 hours)
COOLDOWN = 15           # bars to wait after a sell
TREND_WINDOW = 720      # trend lookback, in bars (12 hours)
TREND_FULL = -0.025     # full size while the 12h return is above this
TREND_HALF = -0.050     # half-size band down to here; below it is washout
TREND_ARM_BAR = 600     # earliest bar for the trend arm (10 hours)
EMA_FAST_N = 60         # fast EMA length, in bars
EMA_SLOW_N = 240        # slow EMA length, in bars
TREND_MIN_360 = 0.005   # trend arm needs the 360-bar return above this
RESCUE_BAR = 1380       # earliest bar for the rescue trade (23 hours)
RESCUE_TP = 0.015       # rescue take-profit
RESCUE_SL = 0.006       # rescue stop-loss
RESCUE_HOLD = 60        # rescue max hold, in bars
LOSS_BRAKE_EQUITY = 850_000_000  # stop new buys if equity falls under $850
MAX_TRADES = 4          # hard cap on completed round trips
WARMUP_MIN_BARS = 60    # no buy before bar 60
LAST_ACTION_BAR = 1438  # no orders on bar 1439 (they can never fill)
MIN_NOTIONAL = 1_000_000  # $1, the harness minimum
MAX_SLIPPAGE_BPS = 150


class Hunter:
    """Keeps the bot's state across bars; one instance serves the whole run."""

    def __init__(self):
        self.closes = []          # every close seen, ints, price-scale-5
        self.entry_close = None   # close at the bar the current buy was decided
        self.entry_bar = None     # bar index of that buy decision
        self.peak = None          # highest close since entry
        self.breakeven_done = False
        self.partial_done = False
        self.rescue_mode = False
        self.trend_arm_used = False
        self.last_sell_bar = None
        self.last_trade_pnl = None  # quote units; negative means the trade lost
        self.trades = 0           # completed round trips
        self.down_buys = 0        # filled down-band buys in this trade cycle
        self.pending_down = False  # the open buy was decided in a down band
        self.trade_cost = 0       # quote units spent on the open position's buys
        self.trade_proceeds = 0   # quote units received from its sells so far
        self.ema_fast = None
        self.ema_slow = None
        self.prev_fast = None
        self.prev_slow = None

    # -- helpers ---------------------------------------------------------
    def _sell_order(self, qty):
        return {"side": "sell", "symbol": SYMBOL, "qty": qty,
                "max_slippage_bps": MAX_SLIPPAGE_BPS}

    def _record_fills(self, state, batch):
        """Fold this batch's fills into the trade book.

        Fills are recorded FIRST, before anything else in the batch is looked
        at, so a fill is never lost when the batch carries no usable bar.
        A filled buy adds its notional to the open trade's cost, and if it was
        decided in a down band it uses one down-band slot (a rejected buy
        never uses a slot). A filled sell adds its proceeds. A round trip is
        counted only when the position is fully closed by a sell the bot
        itself decided (an entry it knows): the harness's end-of-window
        flatten never reaches the bot, and a partial sell leaves the trade
        open, so neither counts here. On a full close the down-band count
        resets to zero, so the cap is per trade cycle, not per run.
        """
        for fill in batch.get("fills") or []:
            if fill.get("symbol") != SYMBOL:
                continue
            status = fill.get("status")
            if fill.get("side") == "buy":
                if status == "filled":
                    self.trade_cost += int(fill.get("notional_quote_units") or 0)
                    if self.pending_down:
                        self.down_buys += 1
                    self.pending_down = False
                else:
                    # The buy never filled: clear the entry it was decided on.
                    # A rejected buy costs no down-band slot and does not
                    # burn the trend arm either: the arm may try again.
                    self.entry_close = None
                    self.entry_bar = None
                    self.peak = None
                    self.breakeven_done = False
                    self.partial_done = False
                    self.rescue_mode = False
                    self.trend_arm_used = False
                    self.pending_down = False
            elif fill.get("side") == "sell":
                if status == "filled":
                    self.trade_proceeds += int(fill.get("notional_quote_units") or 0)
        holding = state.positions.get(SYMBOL)
        if (holding is None or holding.qty == 0) and (
                self.trade_cost > 0 or self.trade_proceeds > 0):
            # The position just closed fully: one completed round trip.
            self.trades += 1
            self.last_trade_pnl = self.trade_proceeds - self.trade_cost
            self.trade_cost = 0
            self.trade_proceeds = 0
            self.entry_close = None
            self.entry_bar = None
            self.peak = None
            self.breakeven_done = False
            self.partial_done = False
            self.rescue_mode = False
            self.trend_arm_used = False
            self.down_buys = 0
            self.pending_down = False

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

    def _ret_ago(self, close, bars_back):
        """The return vs the close bars_back ago (or the first close seen);
        None when it cannot be computed. Never divides by zero."""
        n = len(self.closes)
        if n == 0:
            return None
        ref = self.closes[n - 1 - bars_back] if n > bars_back else self.closes[0]
        if ref <= 0:
            return None
        return (close - ref) / ref

    def _dip_signal(self, close):
        """The dip signal: ('full'|'half', down_band) when it fires, else
        (None, False). The 12h return sets the size, never a veto."""
        if not self.closes:
            return None, False
        hi = max(self.closes[-HI_WINDOW:])
        if hi <= 0:
            return None, False
        dip = (hi - close) / hi
        lo = min(self.closes[-LO_WINDOW:])
        if lo <= 0:
            return None, False
        bounce = (close - lo) / lo
        threshold = DIP_AFTER_LOSS if (self.last_trade_pnl is not None
                                       and self.last_trade_pnl < 0) else DIP
        # Short-term slide guard: when the price fell in the last 2h the
        # market is sliding, not dipping. Demand a much deeper dip and a much
        # stronger bounce, or do not buy at all.
        ret2h = self._ret_ago(close, SLIDE_WINDOW)
        if ret2h is None:
            return None, False
        if ret2h < SLIDE_DEEP:
            if dip >= DIP_SLIDE_DEEP and bounce >= BOUNCE_SLIDE_DEEP:
                return "half", True
            return None, False
        if ret2h < SLIDE_MID:
            if dip >= DIP_SLIDE and bounce >= BOUNCE_SLIDE:
                return "half", True
            return None, False
        ret12 = self._ret_ago(close, TREND_WINDOW)
        if ret12 is None:
            return None, False
        if ret12 > TREND_FULL:
            if dip >= threshold and bounce >= BOUNCE:
                return "full", False
            return None, False
        # Down band: at most 2 filled buys per trade cycle here, so a steady
        # bleed cannot drain us. The count resets on every full close.
        if self.down_buys >= 2:
            return None, False
        if ret12 >= TREND_HALF:
            if dip >= threshold and bounce >= BOUNCE_DOWN:
                return "half", True
            return None, False
        # Washout: deep dip plus strong bounce, half size.
        need = threshold if threshold > DIP_WASHOUT else DIP_WASHOUT
        if dip >= need and bounce >= BOUNCE_DOWN:
            return "half", True
        return None, False

    def _trend_arm_fires(self, close, bar_index):
        """Strict momentum only: EMA-60 crosses above EMA-240, the close sits
        above the fast EMA, and the 360-bar return is positive. The arm
        re-arms after every closed trade, so it can fire on every setup."""
        if bar_index < TREND_ARM_BAR or self.trend_arm_used:
            return False
        if self.prev_fast is None or self.prev_slow is None:
            return False
        if self.ema_fast is None or self.ema_slow is None:
            return False
        if not (self.prev_fast <= self.prev_slow and self.ema_fast > self.ema_slow):
            return False
        if not (close > self.ema_fast):
            return False
        ref360 = self._ret_ago(close, 360)
        if ref360 is None:
            return False
        if ref360 <= TREND_MIN_360:
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
        # Record fills first: a fill must never be lost when the bar is unusable.
        self._record_fills(state, batch)
        close = state.close(SYMBOL)
        if close is None:
            return []
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
        """Exit the open position: trailing stop, partial take, max hold, or the
        rescue trade's fixed exits. The stop is checked first: where a bar
        would trigger both a take and a stop, the stop wins. No orders at all
        on the last bar (1439): they can never fill."""
        if bar_index > LAST_ACTION_BAR:
            return []
        if self.entry_close is None or self.entry_close <= 0:
            return []
        entry_bar = self.entry_bar if self.entry_bar is not None else bar_index
        if self.rescue_mode:
            chg = (close - self.entry_close) / self.entry_close
            if chg >= RESCUE_TP or chg <= -RESCUE_SL or (bar_index - entry_bar) >= RESCUE_HOLD:
                self.last_sell_bar = bar_index
                return [self._sell_order(qty)]
            return []
        if self.peak is None or close > self.peak:
            self.peak = close
        chg = (close - self.entry_close) / self.entry_close
        if not self.breakeven_done and chg >= BREAKEVEN_AT:
            self.breakeven_done = True
        if self.breakeven_done:
            lo15 = min(self.closes[-LO_WINDOW:])
            stop_price = self.entry_close * (1 + BREAKEVEN_STOP)
            trail_price = self.peak * (1 - TRAIL)
            if trail_price > stop_price:
                stop_price = trail_price
            if lo15 > stop_price:
                stop_price = lo15
        else:
            stop_price = self.entry_close * (1 - SL_INIT)
        # Stop first.
        if close <= stop_price:
            self.last_sell_bar = bar_index
            return [self._sell_order(qty)]
        # Bank half at +1.2%, let the rest ride.
        if not self.partial_done and chg >= PARTIAL_AT:
            self.partial_done = True
            half = qty // 2
            if half > 0:
                return [self._sell_order(half)]
            self.last_sell_bar = bar_index
            return [self._sell_order(qty)]
        # Max hold: 12 hours.
        if (bar_index - entry_bar) >= MAX_HOLD:
            self.last_sell_bar = bar_index
            return [self._sell_order(qty)]
        # Early exit: close below the fast EMA after +0.5% unrealized.
        if self.ema_fast is not None and close < self.ema_fast and chg >= 0.005:
            self.last_sell_bar = bar_index
            return [self._sell_order(qty)]
        return []

    def _maybe_buy(self, state, close, bar_index):
        """Buy when flat and a signal fires, unless a guard blocks it."""
        if bar_index < WARMUP_MIN_BARS:
            return []
        if bar_index > LAST_ACTION_BAR:
            return []
        if self.trades >= MAX_TRADES:
            return []
        if self.last_sell_bar is not None and (bar_index - self.last_sell_bar) < COOLDOWN:
            return []
        if state.equity < LOSS_BRAKE_EQUITY:
            return []

        # Evaluate in the spec's order: dip first, then the trend arm, then
        # the rescue. The first signal that fires decides the trade's kind.
        size, down_band = self._dip_signal(close)
        kind = None
        if size is not None:
            kind = "dip"
        elif self._trend_arm_fires(close, bar_index):
            kind = "trend"
            size = "full"
            down_band = False
        elif self.trades == 0 and self._rescue_fires(close, bar_index):
            kind = "rescue"
            size = "full"
            down_band = False
        else:
            return []

        cash = state.cash
        if cash <= 0:
            return []
        # The 0.995 buffer keeps the 4 bps buy fee inside the notional: a
        # full-cash buy can be rejected when the fee lands on top of it.
        if size == "half":
            notional = int(cash * 0.995 * 0.5)
        else:
            notional = int(cash * 0.995)
        if notional < MIN_NOTIONAL:
            return []
        self.entry_close = close
        self.entry_bar = bar_index
        self.peak = close
        self.breakeven_done = False
        self.partial_done = False
        self.pending_down = down_band
        if kind == "trend":
            self.trend_arm_used = True
        if kind == "rescue":
            self.rescue_mode = True
        return [{"side": "buy", "symbol": SYMBOL,
                 "notional_quote_units": notional,
                 "max_slippage_bps": MAX_SLIPPAGE_BPS}]


def main():
    hunter = Hunter()
    makefun_bars.run(hunter.on_batch)


if __name__ == "__main__":
    main()
