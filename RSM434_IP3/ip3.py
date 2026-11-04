"""Shared helpers for RSM434 Industry Project 3: option strategies through market regimes.

The step notebooks import this module so the data handling, regime rules and metrics are defined once.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent
RAW = ROOT / "data" / "raw"
PROCESSED = ROOT / "data" / "processed"
OUTPUT = ROOT / "output"

STRATEGIES = {
    "BXM": {"name": "BuyWrite (ATM call)", "type": "Income", "collateral": "S&P 500"},
    "BXMD": {"name": "30-Delta BuyWrite", "type": "Income", "collateral": "S&P 500"},
    "BXY": {"name": "2% OTM BuyWrite", "type": "Income", "collateral": "S&P 500"},
    "PUT": {"name": "PutWrite (ATM put)", "type": "Income", "collateral": "T-bills"},
    "CNDR": {"name": "Iron Condor", "type": "Income, in a range", "collateral": "T-bills"},
    "BFLY": {"name": "Iron Butterfly", "type": "Income, in a range", "collateral": "T-bills"},
    "CLL": {"name": "95-110 Collar", "type": "Protection", "collateral": "S&P 500"},
    "PPUT": {"name": "5% Put Protection", "type": "Protection", "collateral": "S&P 500"},
}
BENCHMARK = "SPX_TR"
ASSETS = [*STRATEGIES, BENCHMARK]

FRED_SERIES = {
    "VIXCLS": "VIX close",
    "DGS2": "2-year Treasury yield (%)",
    "DGS5": "5-year Treasury yield (%)",
    "DTB3": "3-month T-bill rate (%, discount basis)",
    "BAA10Y": "Moody's Baa corporate yield minus 10-year Treasury (%)",
    "BAMLC0A0CM": "ICE BofA US Corporate IG OAS (%); FRED keeps only ~3 years",
}

CBOE_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/{}_History.csv"
FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={}"
YAHOO_SPX_TR = "^SP500TR"
BLOOMBERG_DIR = RAW / "bloomberg"
BLOOMBERG_STEMS = ["SPXT", "IG_OAS", *STRATEGIES]
PRICE_SERIES = {*STRATEGIES, "SP500TR_YAHOO", "BBG_SPXT", *(f"BBG_{s}" for s in STRATEGIES)}

# The brief's sample starts when Cboe's CLL file does (26 Aug 2008): Aug 2008 is the first month-end reading
# and Sep 2008 the first measured return.
FIRST_READING = pd.Period("2008-08", "M")

# Confirmed bad prints, by strategy feed, found by comparing each index's daily move with the S&P 500's and the
# two feeds with each other. "drop" removes the day (its month-end value then falls back to the previous trading
# day); "splice" treats a permanent jump as a break and rescales the history before it (see apply_data_fixes).
# The Cboe files have further errors not fixed here (CNDR ~1.3% low from Nov 2016 to May 2017; CLL stale on many
# expiry days and ~5% off from 2 to 6 Mar 2020), which is why the Bloomberg feed is preferred.
_BFLY_2022 = {"series": "BFLY", "date": "2022-05-31", "action": "drop",
              "reason": "+6.8% then -5.9% the next day while the S&P 500 fell 0.6% and 0.7%; in both feeds"}
DATA_FIXES = {
    "bloomberg": [_BFLY_2022],
    "cboe": [
        {"series": "BXY", "date": "2013-03-11", "action": "drop",
         "reason": "-25.6% then +34.6% the next day while the S&P 500 moved less than 0.4%"},
        _BFLY_2022,
        {"series": "CLL", "date": "2009-10-16", "action": "splice",
         "reason": "permanent -19.8% jump on a -0.8% S&P 500 day; a collar with 5% put protection cannot lose that"},
        {"series": "CLL", "date": "2020-02-28", "action": "drop",
         "reason": "-5.4% on a -0.8% S&P 500 day, then +12.4% on a +4.6% day"},
    ],
}

VOL_REGIMES = ["Low/Falling", "Low/Rising", "High/Rising", "High/Falling"]
REGIME_NICKNAMES = {
    "Low/Falling": "calm",
    "Low/Rising": "waking up",
    "High/Rising": "stress",
    "High/Falling": "recovery",
}
CREDIT_STATES = ["Tightening", "Widening"]
SLOPE_STATES = ["Flattening", "Steepening"]
REGIME_ORDER = {
    "vol_regime": VOL_REGIMES,
    "regime_credit": [f"{v} | {c}" for v in VOL_REGIMES for c in CREDIT_STATES],
    "regime_slope": [f"{v} | {s}" for v in VOL_REGIMES for s in SLOPE_STATES],
}


# ---------------------------------------------------------------- download

def _get(url: str, expected_start: bytes) -> bytes:
    # FRED stalls on browser-like User-Agent strings; the requests default works for both FRED and Cboe.
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    if not resp.content.lstrip().startswith(expected_start):
        raise ValueError(f"{url} did not return the expected CSV (got {resp.content[:80]!r})")
    return resp.content


def download_all() -> pd.DataFrame:
    """Download every free source into data/raw, untouched, and record what was saved in manifest.json."""
    import yfinance as yf

    saved = []
    for sym in STRATEGIES:
        url = CBOE_URL.format(sym)
        path = RAW / "cboe" / f"{sym}.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_get(url, b"DATE"))
        saved.append({"source": "Cboe", "series": sym, "url": url, "file": str(path.relative_to(ROOT))})

    for sid in FRED_SERIES:
        url = FRED_URL.format(sid)
        path = RAW / "fred" / f"{sid}.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_get(url, b"observation_date"))
        saved.append({"source": "FRED", "series": sid, "url": url, "file": str(path.relative_to(ROOT))})

    close = yf.download(YAHOO_SPX_TR, start="1988-01-01", auto_adjust=False, progress=False)["Close"]
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]
    # Yahoo's row for today is a live intraday price until the close.
    close = close[close.index < pd.Timestamp.today().normalize()]
    path = RAW / "yahoo" / "SP500TR.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    close.rename("SP500TR").rename_axis("date").to_csv(path)
    saved.append({"source": "Yahoo", "series": YAHOO_SPX_TR, "url": "yfinance", "file": str(path.relative_to(ROOT))})

    manifest = {"downloaded_at": datetime.now().isoformat(timespec="seconds"), "files": saved}
    (RAW / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return pd.DataFrame(saved)


# ---------------------------------------------------------------- load

def _two_columns(df: pd.DataFrame, name: str, date_format: str | None = None) -> pd.Series:
    dates = pd.to_datetime(df.iloc[:, 0], format=date_format or "mixed", errors="coerce")
    values = pd.to_numeric(df.iloc[:, 1], errors="coerce")
    keep = dates.notna().to_numpy()
    return pd.Series(values[keep].to_numpy(), index=pd.DatetimeIndex(dates[keep]), name=name).sort_index()


def bloomberg_workbook() -> Path | None:
    """The newest .xlsx in data/raw/bloomberg with sheets named after the Bloomberg series (any file name)."""
    candidates = [
        p for p in BLOOMBERG_DIR.glob("*.xlsx") if not p.name.startswith("~$") and p.stem not in BLOOMBERG_STEMS
    ]
    for path in sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True):
        if set(pd.ExcelFile(path).sheet_names) & set(BLOOMBERG_STEMS):
            return path
    return None


def _read_bloomberg(stem: str, workbook: pd.ExcelFile | None) -> pd.Series | None:
    """A Bloomberg series (date, value) from its own CSV/Excel export, or else from the sheet named `stem` in
    the pull workbook. Header rows are skipped. None if absent or not populated yet."""
    df = None
    for ext in (".csv", ".xlsx", ".xls"):
        path = BLOOMBERG_DIR / f"{stem}{ext}"
        if path.exists():
            df = pd.read_csv(path, header=None) if ext == ".csv" else pd.read_excel(path, header=None)
            break
    if df is None and workbook is not None and stem in workbook.sheet_names:
        df = workbook.parse(stem, header=None)
    if df is None or df.shape[1] < 2:
        return None
    first = df.iloc[:, 0]
    # Excel stores dates as day counts, so cells without a date format come through as plain numbers.
    is_serial = first.map(lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and 20000 <= v <= 80000)
    if is_serial.any():
        serial = pd.to_numeric(first.where(is_serial), errors="coerce")
        df[df.columns[0]] = first.where(~is_serial, pd.to_datetime(serial, unit="D", origin="1899-12-30"))
    series = _two_columns(df, f"BBG_{stem}").dropna()
    # A pull during market hours includes today's live value, not a close.
    series = series[series.index < pd.Timestamp.today().normalize()]
    return series if len(series) else None


def load_raw() -> dict[str, pd.Series]:
    """Every raw series exactly as saved, keyed by series name (Bloomberg ones only if their files exist)."""
    series = {
        sym: _two_columns(pd.read_csv(RAW / "cboe" / f"{sym}.csv"), sym, "%m/%d/%Y") for sym in STRATEGIES
    }
    for sid in FRED_SERIES:
        series[sid] = _two_columns(pd.read_csv(RAW / "fred" / f"{sid}.csv"), sid, "%Y-%m-%d")
    series["SP500TR_YAHOO"] = _two_columns(pd.read_csv(RAW / "yahoo" / "SP500TR.csv"), "SP500TR_YAHOO", "%Y-%m-%d")
    workbook_path = bloomberg_workbook()
    workbook = pd.ExcelFile(workbook_path) if workbook_path else None
    for stem in BLOOMBERG_STEMS:
        bbg = _read_bloomberg(stem, workbook)
        if bbg is not None:
            series[bbg.name] = bbg
    return series


def build_daily(raw: dict[str, pd.Series]) -> tuple[pd.DataFrame, dict[str, str]]:
    """One daily table with the strategies, benchmark (SPX_TR) and credit spread (CREDIT_BP) taken from
    Bloomberg when its data exists and from the free sources otherwise. With Bloomberg strategies, the Cboe
    files stay as CBOE_<symbol> columns for comparison. Also returns which source fed each."""
    clean = {}
    for key, s in raw.items():
        s = s.dropna()
        clean[key] = s[~s.index.duplicated(keep="last")]
    daily = pd.DataFrame(clean).sort_index()
    daily.index.name = "date"

    if all(f"BBG_{sym}" in daily for sym in STRATEGIES):
        for sym in STRATEGIES:
            daily[f"CBOE_{sym}"] = daily[sym]
            daily[sym] = daily[f"BBG_{sym}"]
        strategy_feed, strategy_source = "bloomberg", "Bloomberg (<symbol> Index)"
    else:
        strategy_feed, strategy_source = "cboe", "Cboe website history files"

    if "BBG_SPXT" in daily:
        daily[BENCHMARK] = daily["BBG_SPXT"]
        bench_source = "Bloomberg SPXT Index"
    else:
        daily[BENCHMARK] = daily["SP500TR_YAHOO"]
        bench_source = "Yahoo ^SP500TR (free stand-in)"

    if "BBG_IG_OAS" in daily:
        oas = daily["BBG_IG_OAS"]
        # Exports come in either % or bp; an IG spread never averages below 20bp, so a small median means %.
        daily["CREDIT_BP"] = oas * 100 if oas.median() < 20 else oas
        credit_source = "Bloomberg IG OAS"
    else:
        daily["CREDIT_BP"] = daily["BAA10Y"] * 100
        credit_source = "Moody's Baa minus 10-year Treasury, FRED BAA10Y (free stand-in)"

    sources = {
        "strategy_feed": strategy_feed,
        "strategies": strategy_source,
        "benchmark": bench_source,
        "credit": credit_source,
    }
    return daily, sources


def compare_feeds(daily: pd.DataFrame, month_ends: pd.DatetimeIndex) -> pd.DataFrame:
    """How far the Cboe files (CBOE_<symbol>) differ from the strategy series in use, daily and at month-ends."""
    rows = []
    for sym in STRATEGIES:
        pair = daily[[f"CBOE_{sym}", sym]].dropna().loc[month_ends[0]:month_ends[-1]]
        daily_gap = ((pair / pair.shift(1) - 1).diff(axis=1).iloc[:, 1].abs() * 100).dropna()
        level_gap = (pair[sym] / pair[f"CBOE_{sym}"] - 1).abs() * 100
        levels, _ = sample_at(daily[[f"CBOE_{sym}", sym]], month_ends)
        monthly_gap = ((levels / levels.shift(1) - 1).diff(axis=1).iloc[:, 1].abs() * 100).dropna()
        rows.append({
            "series": sym,
            "days_compared": len(pair),
            "days_return_gap_>0.1pp": int((daily_gap > 0.1).sum()),
            "largest_daily_gap_pp": daily_gap.max(),
            "largest_level_gap_%": level_gap.max(),
            "months_return_gap_>0.1pp": int((monthly_gap > 0.1).sum()),
            "largest_monthly_gap_pp": monthly_gap.max(),
            "worst_month": monthly_gap.idxmax().strftime("%b %Y"),
        })
    return pd.DataFrame(rows).set_index("series")


def _beta_before(daily: pd.DataFrame, sym: str, date: pd.Timestamp, window: int = 60) -> float:
    px = daily[[sym, BENCHMARK]].dropna().loc[:date].iloc[:-1].tail(window + 1)
    r = (px / px.shift(1) - 1).dropna()
    return r[sym].cov(r[BENCHMARK]) / r[BENCHMARK].var()


def apply_data_fixes(daily: pd.DataFrame, fixes: list[dict]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Apply a DATA_FIXES list and return the fixed table plus a log of each fix's before/after daily return.

    A splice keeps every return except the break day's, which is set to the index's beta (prior 60 trading days)
    times the S&P 500's return that day."""
    fixed = daily.copy()
    log = []
    for fix in fixes:
        sym, date = fix["series"], pd.Timestamp(fix["date"])
        px = fixed[sym].dropna()
        i = px.index.get_loc(date)
        before = px.iloc[i] / px.iloc[i - 1] - 1
        if fix["action"] == "drop":
            fixed.loc[date, sym] = np.nan
            after = np.nan
        elif fix["action"] == "splice":
            mkt = fixed[BENCHMARK].dropna()
            mkt_ret = mkt.loc[date] / mkt.loc[:date].iloc[-2] - 1
            after = _beta_before(fixed, sym, date) * mkt_ret
            scale = px.iloc[i] / ((1 + after) * px.iloc[i - 1])
            earlier = fixed.index < date
            fixed.loc[earlier, sym] = fixed.loc[earlier, sym] * scale
        else:
            raise ValueError(f"unknown action {fix['action']!r}")
        log.append({**fix, "return_before_%": before * 100, "return_after_%": after * 100})
    return fixed, pd.DataFrame(log)


def suspicious_days(daily: pd.DataFrame, start, end, threshold_pct: float = 4.0) -> pd.DataFrame:
    """Days where a strategy's move differs from beta x the S&P 500's move by more than threshold_pct, with beta
    from the prior 250 trading days. Option payoffs are nonlinear, so crisis days show up here legitimately;
    the list is for review, not automatic removal."""
    rows = []
    for sym in STRATEGIES:
        px = daily[[sym, BENCHMARK]].dropna()
        r = px / px.shift(1) - 1
        beta = (r[sym].rolling(250, min_periods=60).cov(r[BENCHMARK]) / r[BENCHMARK].rolling(250, min_periods=60).var()).shift(1)
        unexplained = ((r[sym] - beta * r[BENCHMARK]) * 100).loc[start:end]
        for d in unexplained[unexplained.abs() > threshold_pct].index:
            rows.append({
                "series": sym, "date": d.date(), "strategy_%": r.at[d, sym] * 100,
                "sp500_%": r.at[d, BENCHMARK] * 100, "beta": beta[d], "unexplained_%": unexplained[d],
            })
    return pd.DataFrame(rows, columns=["series", "date", "strategy_%", "sp500_%", "beta", "unexplained_%"])


def data_quality(raw: dict[str, pd.Series], start, end) -> pd.DataFrame:
    """Coverage and red flags for each raw series between start and end."""
    calendar = pd.DatetimeIndex(np.unique(np.concatenate([raw[s].dropna().index.values for s in STRATEGIES])))
    rows = []
    for key, s in raw.items():
        obs = s.dropna()
        window = obs.loc[start:end]
        if len(window):
            expected = calendar[(calendar >= max(window.index[0], pd.Timestamp(start))) & (calendar <= pd.Timestamp(end))]
        else:
            expected = calendar[:0]
        is_price = key in PRICE_SERIES
        step = (window / window.shift(1) - 1) * 100 if is_price else window.diff()
        rows.append({
            "series": key,
            "first_date": obs.index.min().date(),
            "last_date": obs.index.max().date(),
            "days_in_window": len(window),
            "cboe_days_missing": len(expected.difference(window.index)),
            "blank_values": int(s.loc[start:end].isna().sum()),
            "duplicate_dates": int(s.index.duplicated().sum()),
            "weekend_dates": int((window.index.dayofweek >= 5).sum()),
            "unchanged_days": int((step == 0).sum()),
            "largest_daily_%_move": step.abs().max() if is_price else np.nan,
            "largest_daily_change": np.nan if is_price else step.abs().max(),
        })
    return pd.DataFrame(rows).set_index("series")


# ---------------------------------------------------------------- month-end panel

def month_end_calendar(daily: pd.DataFrame, last_complete: pd.Period | None = None) -> pd.DatetimeIndex:
    """Last Cboe trading day of every complete calendar month (the current month is excluded by default)."""
    days = daily[list(STRATEGIES)].dropna(how="all").index
    last_day = pd.Series(days, index=days).groupby(days.to_period("M")).max()
    if last_complete is None:
        last_complete = pd.Timestamp.today().to_period("M") - 1
    return pd.DatetimeIndex(last_day[last_day.index <= last_complete].to_numpy(), name="month_end")


def sample_at(daily: pd.DataFrame, dates: pd.DatetimeIndex) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Each column's last observation on or before each date, and how many calendar days old it is."""
    values, age = {}, {}
    for col in daily:
        s = daily[col].dropna()
        values[col] = s.reindex(dates, method="ffill")
        seen = pd.Series(s.index, index=s.index).reindex(dates, method="ffill")
        age[col] = (dates.to_series() - seen).dt.days
    return pd.DataFrame(values, index=dates), pd.DataFrame(age, index=dates)


def monthly_returns(levels: pd.DataFrame) -> pd.DataFrame:
    """Simple return over each month (labelled by the month-end it ends on) for every strategy and the
    benchmark, plus RF: the T-bill return for the month, at the rate known when the month began."""
    prices = levels[ASSETS]
    rets = prices / prices.shift(1) - 1
    rets["RF"] = levels["DTB3"].shift(1) / 100 / 12
    return rets


# ---------------------------------------------------------------- regimes

def _flag(x: pd.Series, positive: str, other: str) -> pd.Series:
    """`positive` where x > 0, `other` where x <= 0, missing where x is missing."""
    return pd.Series(np.where(x > 0, positive, other), index=x.index).where(x.notna())


def compute_signals(daily: pd.DataFrame, month_ends: pd.DatetimeIndex) -> pd.DataFrame:
    """Regime reading at each month-end, built only from daily data dated on or before that month-end."""
    vix = daily["VIXCLS"].dropna()
    inputs = pd.DataFrame({
        "vix": vix,
        "vix_median_12m": vix.rolling("365D").median(),
        "slope_2s5s_bp": (daily["DGS5"] - daily["DGS2"]) * 100,
        "credit_bp": daily["CREDIT_BP"],
    })
    sig, _ = sample_at(inputs, month_ends)
    sig["vix_change_1m"] = sig["vix"].diff()
    sig["credit_change_3m_bp"] = sig["credit_bp"].diff(3)
    sig["slope_change_3m_bp"] = sig["slope_2s5s_bp"].diff(3)

    sig["vol_regime"] = (
        _flag(sig["vix"] - sig["vix_median_12m"], "High", "Low") + "/" + _flag(sig["vix_change_1m"], "Rising", "Falling")
    )
    sig["credit_signal"] = _flag(sig["credit_change_3m_bp"], "Widening", "Tightening")
    sig["slope_signal"] = _flag(sig["slope_change_3m_bp"], "Steepening", "Flattening")
    sig["regime_credit"] = sig["vol_regime"] + " | " + sig["credit_signal"]
    sig["regime_slope"] = sig["vol_regime"] + " | " + sig["slope_signal"]
    return sig


def pair_with_next_month(returns: pd.DataFrame, signals: pd.DataFrame) -> pd.DataFrame:
    """Each month's returns next to the regime read at the previous month-end, the latest reading an
    investor could have acted on. Starts with the first return month after FIRST_READING."""
    reading = signals.shift(1)
    reading.insert(0, "signal_date", signals.index.to_series().shift(1))
    paired = returns.join(reading)
    paired = paired[paired.index.to_period("M") > FIRST_READING]
    prior_month = (paired.index.to_period("M") - 1).to_numpy()
    if not (prior_month == paired["signal_date"].dt.to_period("M").to_numpy()).all():
        raise AssertionError("a return month is paired with a reading other than the previous month-end")
    return paired


def lookahead_check(daily: pd.DataFrame, month_ends: pd.DatetimeIndex, signals: pd.DataFrame) -> pd.DataFrame:
    """Rebuild every month-end reading in `signals` from daily data cut off at that month-end. Returns the
    values that differ from the full-sample reading; an empty table means no rule uses future data."""
    cols = [
        "vix", "vix_median_12m", "vix_change_1m", "credit_change_3m_bp", "slope_change_3m_bp",
        "vol_regime", "regime_credit", "regime_slope",
    ]
    mismatches = []
    for t in signals.index:
        redo = compute_signals(daily.loc[:t], month_ends[month_ends <= t]).loc[t, cols]
        for c in cols:
            full, cut = signals.at[t, c], redo[c]
            if pd.isna(full) and pd.isna(cut):
                continue
            same = full == cut if isinstance(full, str) else bool(np.isclose(full, cut))
            if not same:
                mismatches.append({"month_end": t, "field": c, "full_sample": full, "truncated": cut})
    return pd.DataFrame(mismatches, columns=["month_end", "field", "full_sample", "truncated"])


def sample_halves(index: pd.DatetimeIndex) -> pd.Series:
    """Label each month 'First half' or 'Second half' of the sample, with equal numbers of months."""
    first = np.arange(len(index)) < (len(index) + 1) // 2
    return pd.Series(np.where(first, "First half", "Second half"), index=index, name="half")


def regime_counts(paired: pd.DataFrame, col: str) -> pd.DataFrame:
    """Months in each regime, overall and in each half of the sample."""
    halves = sample_halves(paired.index)
    table = pd.crosstab(paired[col], halves).reindex(
        index=REGIME_ORDER[col], columns=["First half", "Second half"], fill_value=0
    )
    table["All"] = table.sum(axis=1)
    table["Share of months"] = (table["All"] / table["All"].sum()).round(3)
    table.index.name = col
    table.columns.name = None
    return table
