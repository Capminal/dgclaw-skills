"""Shared indicator functions for scalp trading strategies."""


def ema(closes, period):
    """Exponential Moving Average"""
    if len(closes) < period:
        return closes[-1] if closes else 0
    k = 2 / (period + 1)
    result = sum(closes[:period]) / period  # SMA seed
    for c in closes[period:]:
        result = c * k + result * (1 - k)
    return result


def ema_series(closes, period):
    """Full EMA series for ADX computation"""
    if len(closes) < period:
        return closes[:]
    k = 2 / (period + 1)
    series = []
    sma = sum(closes[:period]) / period
    series.append(sma)
    for c in closes[period:]:
        sma = c * k + sma * (1 - k)
        series.append(sma)
    return series


def rsi(closes, period=14):
    """RSI indicator"""
    if len(closes) < period + 1:
        return 50.0  # neutral default
    gains = []
    losses = []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i-1]
        gains.append(max(delta, 0))
        losses.append(max(-delta, 0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def adx(highs, lows, closes, period=14):
    """Average Directional Index"""
    if len(closes) < period * 2:
        return 0.0

    plus_dm = []
    minus_dm = []
    tr_list = []

    for i in range(1, len(closes)):
        h_diff = highs[i] - highs[i-1]
        l_diff = lows[i-1] - lows[i]

        plus_dm.append(max(h_diff, 0) if h_diff > l_diff else 0)
        minus_dm.append(max(l_diff, 0) if l_diff > h_diff else 0)

        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i-1]),
            abs(lows[i] - closes[i-1])
        )
        tr_list.append(tr)

    if len(tr_list) < period:
        return 0.0

    # Smoothed averages
    atr = sum(tr_list[:period]) / period
    smooth_plus = sum(plus_dm[:period]) / period
    smooth_minus = sum(minus_dm[:period]) / period

    dx_values = []

    for i in range(period, len(tr_list)):
        atr = (atr * (period - 1) + tr_list[i]) / period
        smooth_plus = (smooth_plus * (period - 1) + plus_dm[i]) / period
        smooth_minus = (smooth_minus * (period - 1) + minus_dm[i]) / period

        if atr == 0:
            continue
        plus_di = 100 * smooth_plus / atr
        minus_di = 100 * smooth_minus / atr
        di_sum = plus_di + minus_di
        if di_sum == 0:
            continue
        dx = 100 * abs(plus_di - minus_di) / di_sum
        dx_values.append(dx)

    if not dx_values:
        return 0.0

    # ADX = smoothed DX
    adx_val = sum(dx_values[:period]) / min(period, len(dx_values))
    for dx in dx_values[period:]:
        adx_val = (adx_val * (period - 1) + dx) / period

    return adx_val
