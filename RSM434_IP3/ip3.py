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


def _signal_inputs(daily: pd.DataFrame) -> pd.DataFrame:
    """Daily ingredients of the regime rules. The 12-month median is a trailing window, so date t uses no later data."""
    vix = daily["VIXCLS"].dropna()
    return pd.DataFrame({
        "vix": vix,
        "vix_median_12m": vix.rolling("365D").median(),
        "slope_2s5s_bp": (daily["DGS5"] - daily["DGS2"]) * 100,
        "credit_bp": daily["CREDIT_BP"],
    })


def _label_regimes(sig: pd.DataFrame) -> pd.DataFrame:
    """Turn the level and change columns into the regime labels. A zero change counts as falling, tightening, flattening."""
    sig = sig.copy()
    sig["vol_regime"] = (
        _flag(sig["vix"] - sig["vix_median_12m"], "High", "Low") + "/" + _flag(sig["vix_change_1m"], "Rising", "Falling")
    )
    sig["credit_signal"] = _flag(sig["credit_change_3m_bp"], "Widening", "Tightening")
    sig["slope_signal"] = _flag(sig["slope_change_3m_bp"], "Steepening", "Flattening")
    sig["regime_credit"] = sig["vol_regime"] + " | " + sig["credit_signal"]
    sig["regime_slope"] = sig["vol_regime"] + " | " + sig["slope_signal"]
    return sig


def compute_signals(daily: pd.DataFrame, month_ends: pd.DatetimeIndex) -> pd.DataFrame:
    """Regime reading at each month-end, built only from daily data dated on or before that month-end."""
    sig, _ = sample_at(_signal_inputs(daily), month_ends)
    sig["vix_change_1m"] = sig["vix"].diff()
    sig["credit_change_3m_bp"] = sig["credit_bp"].diff(3)
    sig["slope_change_3m_bp"] = sig["slope_2s5s_bp"].diff(3)
    return _label_regimes(sig)


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


# ---------------------------------------------------------------- metrics

# A 95% historical VaR estimated from fewer than this many months is one or two observations.
VAR_MIN_MONTHS = 20
# Credit splits thinner than this are shown and then set aside.
CREDIT_MIN_MONTHS = 12

METRIC_COLUMNS = [
    "n", "ann_return", "ann_vol", "sharpe", "max_drawdown", "var_95", "cvar_95",
    "worst_return", "worst_month", "beta", "var_thin",
]
SIGNAL_FIELDS = [
    "vix", "vix_median_12m", "slope_2s5s_bp", "credit_bp", "vix_change_1m",
    "credit_change_3m_bp", "slope_change_3m_bp", "vol_regime", "credit_signal",
    "slope_signal", "regime_credit", "regime_slope",
]

EPISODES = (
    {"id": "volmageddon", "name": "Volmageddon", "start": "2018-01-26", "end": "2018-02-08"},
    {"id": "covid", "name": "COVID crash", "start": "2020-02-19", "end": "2020-03-23"},
    {"id": "bear_2022", "name": "2022 bear market", "start": "2022-01-03", "end": "2022-10-12"},
    {"id": "gfc", "name": "Financial crisis", "start": "2008-09-15", "end": "2009-03-09"},
)

HMM_STATES = ("Calm", "Transition", "Stress")
HMM_FEATURES = ("log_vix", "vix_change_1m", "credit_change_3m_bp")
HMM_SEED = 434
HMM_RESTARTS = 10

REGIME_COLORS = {
    "Low/Falling": "#1f7a4d",
    "Low/Rising": "#8a6a12",
    "High/Rising": "#9b2c2c",
    "High/Falling": "#1d4e89",
}
STRATEGY_COLORS = {
    "BXM": "#1F4E79",
    "BXMD": "#5B8FB8",
    "BXY": "#8FB8D6",
    "PUT": "#E09F3E",
    "CNDR": "#6B4C9A",
    "BFLY": "#C48AD4",
    "CLL": "#C4504A",
    "PPUT": "#7A2420",
    "SPX_TR": "#1C1915",
}


def load_processed() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """The tables Step 2 saved: daily prices, month-end readings, and returns paired with the prior reading."""
    daily = pd.read_csv(PROCESSED / "daily_clean.csv", parse_dates=["date"], index_col="date")
    readings = pd.read_csv(PROCESSED / "month_end_regimes.csv", parse_dates=["month_end"], index_col="month_end")
    paired = pd.read_csv(
        PROCESSED / "monthly_returns_with_prior_regime.csv", parse_dates=["month_end"], index_col="month_end"
    )
    paired["signal_date"] = pd.to_datetime(paired["signal_date"])
    return daily, readings, paired


def _one_asset_metrics(returns: pd.Series, rf: pd.Series, market: pd.Series) -> dict:
    """Risk and return for one series over the months in `returns`. Drawdown is on wealth stitched in calendar order."""
    aligned = pd.concat({"r": returns, "rf": rf, "m": market}, axis=1).dropna()
    n = len(aligned)
    blank = {
        "n": n, "ann_return": np.nan, "ann_vol": np.nan, "sharpe": np.nan, "max_drawdown": np.nan,
        "var_95": np.nan, "cvar_95": np.nan, "worst_return": np.nan, "worst_month": pd.NaT,
        "beta": np.nan, "var_thin": n < VAR_MIN_MONTHS,
    }
    if n == 0:
        return blank
    r = aligned["r"]
    excess = r - aligned["rf"]
    if (r <= -1).any():
        ann_return = np.nan
    else:
        ann_return = float(np.expm1(np.log1p(r.to_numpy(dtype=float)).sum() * (12 / n)))
    sd = float(r.std(ddof=1)) if n > 1 else np.nan
    ann_vol = sd * np.sqrt(12) if n > 1 else np.nan
    sharpe = float(excess.mean() / sd * np.sqrt(12)) if n > 1 and sd > 0 else np.nan
    wealth = np.cumprod(np.concatenate([[1.0], 1.0 + r.to_numpy(dtype=float)]))
    max_drawdown = float((wealth / np.maximum.accumulate(wealth) - 1.0).min())
    q = float(r.quantile(0.05))
    tail = r[r <= q + 1e-12]
    if tail.empty:
        tail = r.nsmallest(1)
    worst_dt = r.idxmin()
    market_r = aligned["m"]
    if np.allclose(r.to_numpy(dtype=float), market_r.to_numpy(dtype=float)):
        beta = 1.0
    elif n > 1 and float(market_r.std(ddof=1)) > 0:
        beta = float(r.cov(market_r) / market_r.var(ddof=1))
    else:
        beta = np.nan
    return {
        "n": n,
        "ann_return": ann_return,
        "ann_vol": float(ann_vol) if pd.notna(ann_vol) else np.nan,
        "sharpe": sharpe,
        "max_drawdown": max_drawdown,
        "var_95": float(-q),
        "cvar_95": float(-tail.mean()),
        "worst_return": float(r.loc[worst_dt]),
        "worst_month": pd.Timestamp(worst_dt),
        "beta": beta,
        "var_thin": n < VAR_MIN_MONTHS,
    }


def performance_table(paired: pd.DataFrame, group_col: str | None = None) -> pd.DataFrame:
    """One row per regime (or 'All') and series. `group_col` selects vol_regime, regime_credit, or regime_slope."""
    if group_col is None:
        groups = [("All", paired)]
    else:
        groups = [(name, paired[paired[group_col] == name]) for name in REGIME_ORDER[group_col]]
        groups = [(name, frame) for name, frame in groups if len(frame)]
    rows = []
    for name, frame in groups:
        for asset in ASSETS:
            row = {"group": name, "asset": asset}
            row.update(_one_asset_metrics(frame[asset], frame["RF"], frame[BENCHMARK]))
            rows.append(row)
    table = pd.DataFrame(rows, columns=["group", "asset", *METRIC_COLUMNS])
    return table


def pivot_metric(metrics: pd.DataFrame, value: str, columns: list[str] | None = None) -> pd.DataFrame:
    """Strategies down, regimes across, one metric. Row order matches ASSETS."""
    table = metrics.pivot(index="asset", columns="group", values=value)
    if columns is None:
        columns = [c for c in table.columns if c != "All"] or list(table.columns)
    ordered = [c for c in columns if c in table.columns]
    return table.reindex(index=ASSETS, columns=ordered)


def stability_table(paired: pd.DataFrame, group_col: str = "vol_regime") -> pd.DataFrame:
    """Return, Sharpe and beta in each half of the sample, and where the excess-return sign or Sharpe rank flips.

    Rank 1 is the highest Sharpe among the eight strategies and the S&P inside that regime and half.
    A flip is recorded only when both halves have a finite Sharpe."""
    halves = sample_halves(paired.index)
    rows = []
    for name in REGIME_ORDER[group_col]:
        frame = paired[paired[group_col] == name]
        if frame.empty:
            continue
        half_label = halves.loc[frame.index]
        sides = {}
        for label in ("First half", "Second half"):
            side = performance_table(frame.loc[half_label == label]).set_index("asset")
            side["sharpe_rank"] = side["sharpe"].rank(ascending=False, method="min")
            sides[label] = side
        first, second = sides["First half"], sides["Second half"]
        for asset in ASSETS:
            a, b = first.loc[asset], second.loc[asset]
            sign_a = np.sign(a["sharpe"]) if pd.notna(a["sharpe"]) else np.nan
            sign_b = np.sign(b["sharpe"]) if pd.notna(b["sharpe"]) else np.nan
            both = pd.notna(sign_a) and pd.notna(sign_b)
            rows.append({
                "group": name,
                "asset": asset,
                "n_first": int(a["n"]),
                "n_second": int(b["n"]),
                "ann_return_first": a["ann_return"],
                "ann_return_second": b["ann_return"],
                "sharpe_first": a["sharpe"],
                "sharpe_second": b["sharpe"],
                "beta_first": a["beta"],
                "beta_second": b["beta"],
                "sharpe_rank_first": a["sharpe_rank"],
                "sharpe_rank_second": b["sharpe_rank"],
                "excess_sign_flip": bool(both and sign_a != sign_b),
                "sharpe_rank_flip": bool(
                    both and pd.notna(a["sharpe_rank"]) and pd.notna(b["sharpe_rank"])
                    and a["sharpe_rank"] != b["sharpe_rank"]
                ),
                "half_thin": bool(a["n"] < 6 or b["n"] < 6),
            })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- a reading on any date

def _as_timestamp_index(dates) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(pd.to_datetime(dates)).sort_values().unique()


def reading_at(daily: pd.DataFrame, when, month_ends: pd.DatetimeIndex) -> pd.Series:
    """The regime rules on date `when`.

    On a month-end this is the same row as `compute_signals`. Between month-ends the one-month change is
    versus the previous month-end, and the three-month change is versus the month-end three samples back
    on that extended calendar, which is the same diff the month-end rule uses."""
    when = pd.Timestamp(when)
    month_ends = _as_timestamp_index(month_ends)
    if len(month_ends[month_ends == when]):
        when = month_ends[month_ends == when][0]
        dates = month_ends
    else:
        dates = month_ends[month_ends < when].union(pd.DatetimeIndex([when]))
    sig = compute_signals(daily, dates)
    i = sig.index.get_loc(when)
    row = sig.loc[when].copy()
    row["as_of"] = sig.index[i]
    row["anchor_1m"] = sig.index[i - 1] if i >= 1 else pd.NaT
    row["anchor_3m"] = sig.index[i - 3] if i >= 3 else pd.NaT
    return row


def current_reading(daily: pd.DataFrame, as_of=None, month_ends: pd.DatetimeIndex | None = None) -> pd.Series:
    """Regime rules on the latest VIX date, or on `as_of`."""
    if month_ends is None:
        month_ends = month_end_calendar(daily)
    if as_of is None:
        as_of = daily["VIXCLS"].dropna().index.max()
    return reading_at(daily, as_of, month_ends)


def _values_match(left, right) -> bool:
    if pd.isna(left) and pd.isna(right):
        return True
    if isinstance(left, str) or isinstance(right, str):
        return left == right
    return bool(np.isclose(float(left), float(right), rtol=1e-6, atol=1e-6))


def month_end_reading_check(
    daily: pd.DataFrame, month_ends: pd.DatetimeIndex, readings: pd.DataFrame
) -> pd.DataFrame:
    """Where `reading_at` or the saved month-end file disagrees with a fresh `compute_signals`. Empty means they match."""
    fresh = compute_signals(daily, month_ends)
    rows = []
    for t in readings.index:
        got = reading_at(daily, t, month_ends)
        for source, other in (("reading_at", got), ("saved", readings.loc[t])):
            for field in SIGNAL_FIELDS:
                if field not in readings.columns:
                    continue
                if not _values_match(fresh.at[t, field], other[field]):
                    rows.append({
                        "month_end": t, "source": source, "field": field,
                        "fresh": fresh.at[t, field], "other": other[field],
                    })
    return pd.DataFrame(rows, columns=["month_end", "source", "field", "fresh", "other"])


# ---------------------------------------------------------------- case studies

def _window_prices(daily: pd.DataFrame, start, end) -> pd.DataFrame:
    return daily.loc[pd.Timestamp(start):pd.Timestamp(end), ASSETS]


def episode_wealth(prices: pd.DataFrame) -> pd.DataFrame:
    """Index level, rebased to 100 at each series' first print inside the window."""
    out = pd.DataFrame(index=prices.index)
    for asset in prices.columns:
        series = prices[asset].dropna()
        if series.empty:
            continue
        out[asset] = prices[asset] / series.iloc[0] * 100
    return out


def episode_stats(prices: pd.DataFrame) -> pd.DataFrame:
    """Total return, peak-to-trough drawdown and worst day for each series inside a window."""
    rows = []
    for asset in [c for c in ASSETS if c in prices.columns]:
        series = prices[asset].dropna()
        if len(series) < 2:
            continue
        wealth = series / series.iloc[0]
        daily_ret = series.pct_change().dropna()
        worst_dt = daily_ret.idxmin()
        rows.append({
            "asset": asset,
            "first": series.index[0],
            "last": series.index[-1],
            "days": int(len(series)),
            "total_return": float(series.iloc[-1] / series.iloc[0] - 1),
            "max_drawdown": float((wealth / wealth.cummax() - 1).min()),
            "worst_day": float(daily_ret.loc[worst_dt]),
            "worst_day_date": pd.Timestamp(worst_dt),
        })
    return pd.DataFrame(rows).set_index("asset").reindex([a for a in ASSETS if a in prices.columns])


def episode_context(daily: pd.DataFrame, month_ends: pd.DatetimeIndex, readings: pd.DataFrame, episode: dict) -> dict:
    """The last month-end before the window, the reading on the last session before it, and month-ends inside it.

    Only the prior month-end was knowable before the window opened. Month-ends inside the window are concurrent.
    The eve reading applies the same rules on the last VIX session before the start, so a move that happens
    after the previous month-end and before the window is visible."""
    start, end = pd.Timestamp(episode["start"]), pd.Timestamp(episode["end"])
    prior_dt = readings.index[readings.index < start][-1]
    prior = readings.loc[prior_dt]
    during = readings.loc[(readings.index >= start) & (readings.index <= end), "vol_regime"]
    vix_days = daily["VIXCLS"].dropna().index
    eve = vix_days[vix_days < start][-1]
    eve_reading = reading_at(daily, eve, month_ends)
    vix = daily["VIXCLS"].dropna().loc[start:end]
    spx = daily.loc[start:end, BENCHMARK].dropna()
    return {
        "id": episode["id"],
        "episode": episode["name"],
        "start": start,
        "end": end,
        "prior_date": pd.Timestamp(prior_dt),
        "prior_vix": float(prior["vix"]),
        "prior_vix_median": float(prior["vix_median_12m"]),
        "prior_vol_regime": prior["vol_regime"],
        "prior_credit": prior["credit_signal"],
        "prior_slope": prior["slope_signal"],
        "eve_date": pd.Timestamp(eve),
        "eve_is_month_end": bool(len(month_ends[month_ends == eve])),
        "eve_vix": float(eve_reading["vix"]),
        "eve_vix_change_1m": float(eve_reading["vix_change_1m"]),
        "eve_vol_regime": eve_reading["vol_regime"],
        "eve_credit": eve_reading["credit_signal"],
        "during": "; ".join(f"{dt:%b %Y} {lab}" for dt, lab in during.items()),
        "vix_max": float(vix.max()),
        "vix_max_date": pd.Timestamp(vix.idxmax()),
        "spx_return": float(spx.iloc[-1] / spx.iloc[0] - 1),
    }


def run_episodes(
    daily: pd.DataFrame, month_ends: pd.DatetimeIndex, readings: pd.DataFrame, episodes=EPISODES
) -> tuple[pd.DataFrame, dict[str, pd.DataFrame], pd.DataFrame]:
    """Stats, wealth paths and regime context for each stress window."""
    stats, wealth, context = [], {}, []
    for episode in episodes:
        prices = _window_prices(daily, episode["start"], episode["end"])
        table = episode_stats(prices)
        table.insert(0, "episode", episode["name"])
        table.insert(0, "id", episode["id"])
        stats.append(table.reset_index())
        wealth[episode["id"]] = episode_wealth(prices)
        context.append(episode_context(daily, month_ends, readings, episode))
    return pd.concat(stats, ignore_index=True), wealth, pd.DataFrame(context)


# ---------------------------------------------------------------- hidden Markov model

def _logsumexp(values: np.ndarray, axis: int) -> np.ndarray:
    finite_max = np.max(values, axis=axis, keepdims=True)
    finite_max = np.where(np.isfinite(finite_max), finite_max, 0.0)
    total = np.sum(np.exp(values - finite_max), axis=axis, keepdims=True)
    return np.squeeze(finite_max + np.log(total), axis=axis)


def _gaussian_logpdf(features: np.ndarray, mean: np.ndarray, cov: np.ndarray) -> np.ndarray:
    dim = features.shape[1]
    cov = np.array(cov, dtype=float, copy=True)
    jitter = 1e-8
    sign, logdet = np.linalg.slogdet(cov)
    if sign <= 0:
        cov = cov + np.eye(dim) * jitter
        sign, logdet = np.linalg.slogdet(cov)
    diff = features - mean
    quad = np.einsum("ti,ij,tj->t", diff, np.linalg.inv(cov), diff)
    return -0.5 * (dim * np.log(2 * np.pi) + logdet + quad)


def _permute_hmm(model, order: np.ndarray) -> None:
    """Reorder states in place. `order[new] = old`, so index 0 becomes the lowest-VIX state."""
    model.startprob_ = model.startprob_[order]
    model.transmat_ = model.transmat_[np.ix_(order, order)]
    model.means_ = model.means_[order]
    model.covars_ = model.covars_[order]


def _filtered_probs(model, features: np.ndarray) -> np.ndarray:
    """P(state_t | observations through t). This does not use later months, unlike the Viterbi path."""
    log_emit = np.column_stack([
        _gaussian_logpdf(features, model.means_[k], model.covars_[k]) for k in range(model.n_components)
    ])
    n_obs, n_states = log_emit.shape
    log_alpha = np.empty((n_obs, n_states))
    log_alpha[0] = np.log(np.clip(model.startprob_, 1e-300, None)) + log_emit[0]
    log_trans = np.log(np.clip(model.transmat_, 1e-300, None))
    for t in range(1, n_obs):
        log_alpha[t] = log_emit[t] + _logsumexp(log_alpha[t - 1][:, None] + log_trans, axis=0)
    return np.exp(log_alpha - _logsumexp(log_alpha, axis=1)[:, None])


def fit_hmm(readings: pd.DataFrame, n_restarts: int = HMM_RESTARTS, seed: int = HMM_SEED) -> dict:
    """3-state Gaussian HMM on log VIX, the one-month VIX change and the three-month credit change.

    Features are standardized with the full-sample mean and standard deviation, so this is an in-sample
    comparison with the rules, not a signal that could have been computed in real time. States are named
    Calm, Transition and Stress by ascending mean log VIX. `hmm_state` is the Viterbi path. `p_*` columns
    are filtered probabilities."""
    import os
    import warnings

    os.environ.setdefault("LOKY_MAX_CPU_COUNT", "1")
    warnings.filterwarnings("ignore", message="A NumPy version")
    warnings.filterwarnings("ignore", message="Could not find the number of physical cores")
    from hmmlearn.hmm import GaussianHMM

    frame = readings[["vix", "vix_change_1m", "credit_change_3m_bp", "vol_regime"]].dropna().copy()
    frame["log_vix"] = np.log(frame["vix"].astype(float))
    raw = frame[list(HMM_FEATURES)].to_numpy(dtype=float)
    mu = raw.mean(axis=0)
    sd = raw.std(axis=0, ddof=0)
    features = (raw - mu) / sd

    best_model, best_score, best_seed, best_converged = None, -np.inf, None, False
    for restart in range(n_restarts):
        model = GaussianHMM(
            n_components=3, covariance_type="full", n_iter=200, tol=1e-4,
            random_state=seed + restart, min_covar=1e-3, verbose=False,
        )
        model.fit(features)
        score = float(model.score(features))
        converged = bool(getattr(getattr(model, "monitor_", None), "converged", False))
        if score > best_score:
            best_model, best_score, best_seed, best_converged = model, score, seed + restart, converged

    order = np.argsort(best_model.means_[:, 0])
    _permute_hmm(best_model, order)
    viterbi = best_model.predict(features)
    filtered = _filtered_probs(best_model, features)
    frame["hmm_state"] = [HMM_STATES[k] for k in viterbi]
    for k, name in enumerate(HMM_STATES):
        frame[f"p_{name}"] = filtered[:, k]
    counts = pd.crosstab(frame["vol_regime"], frame["hmm_state"])
    counts = counts.reindex(index=list(VOL_REGIMES), columns=list(HMM_STATES), fill_value=0)
    share = counts.div(counts.sum(axis=1).replace(0, np.nan), axis=0)
    return {
        "frame": frame,
        "counts": counts,
        "share": share,
        "score": best_score,
        "converged": best_converged,
        "seed": best_seed,
        "mu": mu,
        "sd": sd,
        "model": best_model,
    }


def attach_hmm_to_episodes(context: pd.DataFrame, hmm_frame: pd.DataFrame) -> pd.DataFrame:
    """Filtered state probabilities on the last month-end before each episode. These do not use later data."""
    out = context.copy()
    lookup = hmm_frame[["hmm_state", "p_Calm", "p_Transition", "p_Stress"]]
    for column, source in (
        ("prior_hmm_state", "hmm_state"),
        ("prior_p_calm", "p_Calm"),
        ("prior_p_transition", "p_Transition"),
        ("prior_p_stress", "p_Stress"),
    ):
        out[column] = out["prior_date"].map(lookup[source])
    return out


# ---------------------------------------------------------------- workbook and monitor

def _excel_labels(frame: pd.DataFrame) -> pd.DataFrame:
    names = {
        "group": "Regime", "asset": "Series", "n": "Months", "ann_return": "Ann. return",
        "ann_vol": "Ann. volatility", "sharpe": "Sharpe", "max_drawdown": "Max drawdown",
        "var_95": "95% VaR", "cvar_95": "95% CVaR", "worst_return": "Worst month",
        "worst_month": "Worst month date", "beta": "Beta to S&P", "var_thin": "VaR thin (n<20)",
        "n_first": "Months, first half", "n_second": "Months, second half",
        "ann_return_first": "Ann. return, first half", "ann_return_second": "Ann. return, second half",
        "sharpe_first": "Sharpe, first half", "sharpe_second": "Sharpe, second half",
        "beta_first": "Beta, first half", "beta_second": "Beta, second half",
        "sharpe_rank_first": "Sharpe rank, first half", "sharpe_rank_second": "Sharpe rank, second half",
        "excess_sign_flip": "Excess-return sign flips", "sharpe_rank_flip": "Sharpe rank flips",
        "half_thin": "A half has fewer than 6 months",
        "id": "Id", "episode": "Episode", "first": "First print", "last": "Last print", "days": "Prints",
        "total_return": "Total return", "worst_day": "Worst day", "worst_day_date": "Worst day date",
        "slice": "History used",
    }
    return frame.rename(columns=names)


_PCT_HEADERS = {
    "Ann. return", "Ann. volatility", "Max drawdown", "95% VaR", "95% CVaR", "Worst month",
    "Ann. return, first half", "Ann. return, second half", "Total return", "Worst day",
}
_NUM_HEADERS = {
    "Sharpe", "Beta to S&P", "Sharpe, first half", "Sharpe, second half",
    "Beta, first half", "Beta, second half", "Sharpe rank, first half", "Sharpe rank, second half",
}
_DATE_HEADERS = {
    "Worst month date", "First print", "Last print", "Worst day date", "signal_date", "as_of",
    "anchor_1m", "anchor_3m", "prior_date", "eve_date", "vix_max_date", "start", "end",
    "month_end", "last_month_end",
}


def _is_date_header(label) -> bool:
    if not isinstance(label, str):
        return False
    return label in _DATE_HEADERS or label.endswith("_date")


def _write_sheet(
    writer: pd.ExcelWriter, name: str, frame: pd.DataFrame, index: bool = False, percent_body: bool = False
) -> None:
    from openpyxl.styles import Alignment, Font

    frame.to_excel(writer, sheet_name=name, index=index)
    ws = writer.sheets[name]
    ws.freeze_panes = "B2" if index else "A2"
    ws.auto_filter.ref = ws.dimensions
    ws.row_dimensions[1].height = 18
    for cell in ws[1]:
        cell.font = Font(bold=True)
    if name == "Definitions":
        ws.column_dimensions["A"].width = 140
        for row in ws.iter_rows(min_row=2, max_col=1):
            for cell in row:
                cell.alignment = Alignment(wrap_text=True, vertical="top")
        return
    header = {cell.value: cell.column for cell in ws[1]}
    for label, col in header.items():
        width = 72 if label == "during" else min(max(len(str(label)) + 2, 12), 36)
        ws.column_dimensions[cell_letter(col)].width = width
        number_format = None
        if percent_body and col > 1:
            number_format = "0.0%"
        elif label in _PCT_HEADERS:
            number_format = "0.0%"
        elif label in _NUM_HEADERS or (isinstance(label, str) and label.startswith("p_")):
            number_format = "0.00"
        elif _is_date_header(label):
            number_format = "YYYY-MM-DD"
        if number_format is None:
            continue
        for row in range(2, ws.max_row + 1):
            value = ws.cell(row, col).value
            if isinstance(value, (int, float, datetime)) and not isinstance(value, bool):
                ws.cell(row, col).number_format = number_format


def cell_letter(col: int) -> str:
    from openpyxl.utils import get_column_letter
    return get_column_letter(col)


def write_compiled_workbook(path: Path, payload: dict) -> None:
    """The spreadsheet deliverable: panel, metrics, stability, case studies, HMM and the monitor snapshot."""
    path.parent.mkdir(parents=True, exist_ok=True)
    notes = pd.DataFrame({"Note": payload["notes"]})
    sheets = [
        ("Definitions", notes, False, False),
        ("Monthly panel", payload["paired"].reset_index(), False, False),
        ("Month-end readings", payload["readings"].reset_index(), False, False),
        ("Metrics overall", _excel_labels(payload["metrics_all"]), False, False),
        ("Metrics by vol regime", _excel_labels(payload["metrics_vol"]), False, False),
        ("Metrics vol x credit", _excel_labels(payload["metrics_credit"]), False, False),
        ("Metrics vol x slope", _excel_labels(payload["metrics_slope"]), False, False),
        ("Half-sample stability", _excel_labels(payload["stability"]), False, False),
        ("Case studies", _excel_labels(payload["episode_stats"]), False, False),
        ("Case context", payload["episode_context"], False, False),
        ("HMM states", payload["hmm"].reset_index(), False, False),
        ("HMM counts", payload["hmm_counts"], True, False),
        ("HMM share", payload["hmm_share"], True, True),
        ("Monitor", payload["monitor_reading"], False, False),
        ("Monitor history", _excel_labels(payload["monitor_history"]), False, False),
    ]
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        for name, frame, index, percent_body in sheets:
            _write_sheet(writer, name, frame, index=index, percent_body=percent_body)


def monitor_history(paired: pd.DataFrame, reading: pd.Series) -> pd.DataFrame:
    """How each series behaved in later months after the same volatility regime, and after the credit split."""
    vol = reading["vol_regime"]
    credit = reading["credit_signal"]
    by_vol = performance_table(paired[paired["vol_regime"] == vol]).assign(slice=vol)
    by_credit = performance_table(
        paired[(paired["vol_regime"] == vol) & (paired["credit_signal"] == credit)]
    ).assign(slice=f"{vol} | {credit}")
    by_vol["group"] = vol
    by_credit["group"] = f"{vol} | {credit}"
    return pd.concat([by_vol, by_credit], ignore_index=True)


def _fmt_date(value) -> str:
    if pd.isna(value):
        return "—"
    stamp = pd.Timestamp(value)
    return f"{stamp.day} {stamp:%b %Y}"


def _monitor_styles():
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    thin = Border(
        left=Side(style="thin", color="E4DFD6"),
        right=Side(style="thin", color="E4DFD6"),
        top=Side(style="thin", color="E4DFD6"),
        bottom=Side(style="thin", color="E4DFD6"),
    )
    return {
        "title": Font(name="Calibri", size=20, bold=True, color="1C1915"),
        "kicker": Font(name="Calibri", size=11, color="6B645C"),
        "body": Font(name="Calibri", size=11, color="1C1915"),
        "section": Font(name="Calibri", size=13, bold=True, color="1C1915"),
        "header": Font(name="Calibri", size=10, bold=True, color="FFFFFF"),
        "header_fill": PatternFill("solid", fgColor="4A453F"),
        "label": Font(name="Calibri", size=11, color="6B645C"),
        "value": Font(name="Calibri", size=14, bold=True, color="1C1915"),
        "white": Font(name="Calibri", size=14, bold=True, color="FFFFFF"),
        "wrap": Alignment(wrap_text=True, vertical="center"),
        "left": Alignment(vertical="center"),
        "right": Alignment(horizontal="right", vertical="center"),
        "thin": thin,
    }


def _paint(cell, font, fill=None, align=None, border=None, fmt=None):
    cell.font = font
    if fill is not None:
        cell.fill = fill
    if align is not None:
        cell.alignment = align
    if border is not None:
        cell.border = border
    if fmt is not None:
        cell.number_format = fmt


def _write_monitor_table(ws, start: int, frame: pd.DataFrame, styles: dict) -> int:
    """Write one strategy history table. Returns the row after the last data row."""
    headers = ["Series", "Name", "Type", "Months", "Ann. return", "Sharpe", "Worst month", "Beta"]
    for col, header in enumerate(headers, start=1):
        cell = ws.cell(start, col, header)
        _paint(cell, styles["header"], styles["header_fill"], styles["left"], styles["thin"])
    ordered = frame.set_index("asset").reindex(ASSETS)
    for offset, asset in enumerate(ASSETS, start=1):
        row = ordered.loc[asset]
        kind = "Benchmark" if asset == BENCHMARK else STRATEGIES[asset]["type"]
        name = "S&P 500 total return" if asset == BENCHMARK else STRATEGIES[asset]["name"]
        values = [
            asset, name, kind, int(row["n"]), float(row["ann_return"]), float(row["sharpe"]),
            float(row["worst_return"]), float(row["beta"]),
        ]
        formats = [None, None, None, "0", "0.0%", "0.00", "0.0%", "0.00"]
        aligns = [styles["left"], styles["left"], styles["left"]] + [styles["right"]] * 5
        for col, (value, fmt, align) in enumerate(zip(values, formats, aligns), start=1):
            cell = ws.cell(start + offset, col, value)
            _paint(cell, styles["body"], align=align, border=styles["thin"], fmt=fmt)
        if bool(row["var_thin"]):
            ws.cell(start + offset, 4).value = f"{int(row['n'])} *"
            ws.cell(start + offset, 4).number_format = "@"
            ws.cell(start + offset, 4).alignment = styles["right"]
    return start + len(ASSETS) + 1


def write_regime_monitor(
    path: Path, reading: pd.Series, last_month_end: pd.Series, history: pd.DataFrame
) -> None:
    """Excel monitor: the latest rules-based reading, and how the strategies did after that regime."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    path.parent.mkdir(parents=True, exist_ok=True)
    styles = _monitor_styles()
    regime = reading["vol_regime"]
    nick = REGIME_NICKNAMES[regime]
    as_of = pd.Timestamp(reading["as_of"])
    anchor = pd.Timestamp(reading["anchor_1m"])
    change_label = (
        "VIX change over the month"
        if (as_of - anchor).days > 20
        else f"VIX change since {_fmt_date(anchor)}"
    )
    level = "above" if reading["vix"] > reading["vix_median_12m"] else "at or below"
    vol_hist = history[history["slice"] == regime]
    credit_label = f"{regime} | {reading['credit_signal']}"
    credit_hist = history[history["slice"] == credit_label]
    credit_n = int(credit_hist["n"].iloc[0]) if len(credit_hist) else 0
    last_dt = pd.Timestamp(last_month_end.name)
    same = (
        last_month_end["vol_regime"] == regime and last_month_end["credit_signal"] == reading["credit_signal"]
    )
    month_end_line = (
        f"Last month-end ({_fmt_date(last_dt)}) has the same volatility and credit reading."
        if same else
        f"Last month-end ({_fmt_date(last_dt)}) was {last_month_end['vol_regime']}, "
        f"credit {str(last_month_end['credit_signal']).lower()}."
    )
    built_at = datetime.now()
    built = f"{built_at.day} {built_at:%b %Y %H:%M}"
    regime_fill = PatternFill("solid", fgColor=REGIME_COLORS[regime].lstrip("#"))

    wb = Workbook()
    ws = wb.active
    ws.title = "Monitor"
    ws.sheet_view.showGridLines = False
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.page_setup.paperSize = ws.PAPERSIZE_TABLOID
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_setup.horizontalCentered = True
    ws.sheet_properties.tabColor = REGIME_COLORS[regime].lstrip("#")
    ws.oddHeader.left.text = "RSM434 · Regime monitor"
    ws.oddFooter.right.text = f"Built {built}"

    widths = [34, 28, 22, 12, 14, 12, 14, 12]
    for index, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(index)].width = width

    ws.merge_cells("A1:H1")
    _paint(ws["A1"], styles["title"], align=styles["left"])
    ws["A1"] = "Regime monitor"
    ws.row_dimensions[1].height = 28
    ws.merge_cells("A2:H2")
    _paint(ws["A2"], styles["kicker"])
    ws["A2"] = "RSM434 · Option strategies through market regimes"
    ws.merge_cells("A3:H3")
    _paint(ws["A3"], styles["body"])
    ws["A3"] = f"Reading as of {_fmt_date(as_of)}, the latest saved VIX close. This is not a live quote."

    _paint(ws["A5"], styles["white"], regime_fill, Alignment(horizontal="center", vertical="center"))
    ws["A5"] = regime
    ws.row_dimensions[5].height = 24
    _paint(ws["B5"], Font(name="Calibri", size=14, italic=True, color="1C1915"), align=styles["left"])
    ws["B5"] = nick
    ws.merge_cells("C5:H5")
    _paint(ws["C5"], styles["body"], align=styles["left"])
    ws["C5"] = (
        f"Credit {str(reading['credit_signal']).lower()}  ·  "
        f"2s5s {str(reading['slope_signal']).lower()}"
    )
    ws.merge_cells("A6:H6")
    _paint(ws["A6"], styles["body"])
    ws["A6"] = month_end_line

    ws.merge_cells("A8:H8")
    _paint(ws["A8"], styles["section"])
    ws["A8"] = "Signals"
    signals = [
        ("VIX", float(reading["vix"]), "0.00", f"VIX is {level} its trailing 12-month median"),
        ("Trailing 12-month median", float(reading["vix_median_12m"]), "0.00", ""),
        (change_label, float(reading["vix_change_1m"]), "+0.00", ""),
        ("Credit spread, 3-month change (bp)", float(reading["credit_change_3m_bp"]), "0.0", ""),
        (
            "2s5s slope, 3-month change (bp)",
            float(reading["slope_change_3m_bp"]),
            "0.0",
            "Tracked. Not used in the ranking.",
        ),
    ]
    for offset, (label, value, fmt, note) in enumerate(signals):
        row = 9 + offset
        _paint(ws.cell(row, 1, label), styles["label"], align=styles["left"])
        cell = ws.cell(row, 2, value)
        _paint(cell, styles["value"], align=styles["right"], fmt=fmt)
        if note:
            ws.merge_cells(start_row=row, start_column=3, end_row=row, end_column=8)
            _paint(ws.cell(row, 3, note), styles["label"])

    section = 15
    ws.merge_cells(start_row=section, start_column=1, end_row=section, end_column=8)
    _paint(ws.cell(section, 1, f"After {regime} readings"), styles["section"])
    ws.merge_cells(start_row=section + 1, start_column=1, end_row=section + 1, end_column=8)
    _paint(
        ws.cell(
            section + 1, 1,
            "Each figure uses the months that followed this volatility regime. Return is geometric and "
            "annualized. Sharpe is excess over T-bills. Beta is versus the S&P 500 total return.",
        ),
        styles["body"], align=styles["wrap"],
    )
    ws.row_dimensions[section + 1].height = 32
    next_row = _write_monitor_table(ws, section + 3, vol_hist, styles)

    credit_row = next_row + 1
    ws.merge_cells(start_row=credit_row, start_column=1, end_row=credit_row, end_column=8)
    _paint(ws.cell(credit_row, 1, "Credit modifier"), styles["section"])
    ws.merge_cells(start_row=credit_row + 1, start_column=1, end_row=credit_row + 1, end_column=8)
    if credit_n >= CREDIT_MIN_MONTHS:
        intro = (
            f"Same volatility regime with credit {str(reading['credit_signal']).lower()}: "
            f"{credit_n} months. This split is large enough to look at. It remains a modifier on the "
            "four-regime grid."
        )
        _paint(ws.cell(credit_row + 1, 1, intro), styles["body"], align=styles["wrap"])
        ws.row_dimensions[credit_row + 1].height = 32
        next_row = _write_monitor_table(ws, credit_row + 3, credit_hist, styles)
    else:
        intro = (
            f"Credit is {str(reading['credit_signal']).lower()}, and that split of {regime} has "
            f"{credit_n} months. Cells under {CREDIT_MIN_MONTHS} months are set aside, so the history "
            "above is the volatility regime on its own."
        )
        _paint(ws.cell(credit_row + 1, 1, intro), styles["body"], align=styles["wrap"])
        ws.row_dimensions[credit_row + 1].height = 32
        next_row = credit_row + 2

    note_row = next_row + 1
    ws.merge_cells(start_row=note_row, start_column=1, end_row=note_row, end_column=8)
    _paint(ws.cell(note_row, 1, "How to use this sheet"), styles["section"])
    note = (
        "The indices are hypothetical. History before each launch date is back-calculated, and nothing "
        "here includes trading costs, fees or taxes. Buy-writes and the collar hold the S&P 500. The "
        "put-write, condor and butterfly hold T-bills, so a higher return with a higher beta is equity "
        f"exposure. The four volatility regimes are the decision grid. Credit is a modifier, and only when "
        f"the cell has at least {CREDIT_MIN_MONTHS} months. A star on Months means the cell has fewer than "
        f"{VAR_MIN_MONTHS} months, so a 95% VaR from it would rest on one or two observations. Re-run the "
        f"Step 4 notebook after a newer Bloomberg file is saved in data/raw/bloomberg to refresh this "
        f"workbook. Built {built}."
    )
    ws.merge_cells(start_row=note_row + 1, start_column=1, end_row=note_row + 1, end_column=8)
    _paint(ws.cell(note_row + 1, 1, note), styles["kicker"], align=styles["wrap"])
    ws.row_dimensions[note_row + 1].height = 62
    ws.freeze_panes = "A8"
    ws.print_area = f"A1:H{note_row + 1}"
    ws.page_setup.horizontalCentered = True
    wb.save(path)


def monitor_snapshot(reading: pd.Series, last_month_end: pd.Series) -> pd.DataFrame:
    """One row for the Monitor sheet: the latest reading and the last official month-end."""
    return pd.DataFrame([{
        "as_of": pd.Timestamp(reading["as_of"]),
        "vol_regime": reading["vol_regime"],
        "credit_signal": reading["credit_signal"],
        "slope_signal": reading["slope_signal"],
        "vix": float(reading["vix"]),
        "vix_median_12m": float(reading["vix_median_12m"]),
        "vix_change_1m": float(reading["vix_change_1m"]),
        "credit_change_3m_bp": float(reading["credit_change_3m_bp"]),
        "slope_change_3m_bp": float(reading["slope_change_3m_bp"]),
        "anchor_1m": pd.Timestamp(reading["anchor_1m"]),
        "anchor_3m": pd.Timestamp(reading["anchor_3m"]),
        "last_month_end": pd.Timestamp(last_month_end.name),
        "last_month_end_vol_regime": last_month_end["vol_regime"],
        "last_month_end_credit": last_month_end["credit_signal"],
        "last_month_end_slope": last_month_end["slope_signal"],
    }])


def workbook_notes(paired: pd.DataFrame, reading: pd.Series, hmm: dict) -> list[str]:
    """Plain-language definitions for the first sheet of the workbook."""
    start, end = paired.index.min(), paired.index.max()
    as_of = pd.Timestamp(reading["as_of"])
    return [
        f"Sample: {len(paired)} return months, {start:%b %Y} to {end:%b %Y}. Each month's return is paired with the regime read at the previous month-end (signal_date). The label uses no later information.",
        "Ann. return is geometric: (product of 1 + monthly return) raised to 12/n, minus 1.",
        "Ann. volatility is the sample standard deviation of monthly returns times sqrt(12).",
        "Sharpe is the mean of (monthly return minus that month's T-bill return), divided by the standard deviation of the monthly return, times sqrt(12). The T-bill return is the 3-month rate known at the start of the month, divided by 12.",
        "Max drawdown is the worst peak-to-trough decline of wealth compounded from the months in that row, in calendar order, starting from 1. It is negative. Months in one regime are not always consecutive, so this is the conditional track record.",
        f"95% VaR is minus the 5th percentile of monthly returns, as a positive loss. 95% CVaR is minus the average of the months at or below that percentile. VaR thin is true when the cell has fewer than {VAR_MIN_MONTHS} months.",
        "Beta is the slope of the series on the S&P 500 total return over the same months. Buy-writes and the collar hold the S&P 500. PUT, CNDR and BFLY hold T-bills.",
        "Volatility regime: VIX above or below its trailing 12-month median, and VIX up or down versus the previous month-end. A zero change counts as falling. Credit is the sign of the 3-month change in the investment-grade spread. The 2s5s slope is reported alongside it. Credit is the added signal. The slope is the comparison that does less for the stress decision.",
        f"A credit split with fewer than {CREDIT_MIN_MONTHS} months is shown and then set aside.",
        "Case-study total returns run from the first print in the window to the last. Drawdown is peak to trough inside the window. The prior month-end is the last reading knowable before the window opened. The eve reading applies the same rules on the last VIX session before the start. Month-ends inside the window are concurrent.",
        (
            f"HMM: 3-state Gaussian model on log VIX, the one-month VIX change and the 3-month credit change, "
            f"standardized with the full-sample mean and standard deviation. In-sample log-likelihood {hmm['score']:.1f}. "
            f"Converged: {hmm['converged']}. Best restart seed {hmm['seed']}. hmm_state is the Viterbi path and uses the whole sample. "
            "p_Calm, p_Transition and p_Stress are filtered probabilities and use only observations through that month. "
            "On the first month the filtered probability is the estimated initial distribution."
        ),
        "The strategy indices are hypothetical. History before each launch is back-calculated. Figures include no trading costs, fees or taxes.",
        (
            f"Monitor as of {_when(as_of, year=True)}: {reading['vol_regime']}, credit {reading['credit_signal']}, "
            f"2s5s {reading['slope_signal']}. VIX {float(reading['vix']):.2f} versus a trailing median of "
            f"{float(reading['vix_median_12m']):.2f}."
        ),
    ]


def _pct(value, digits: int = 1) -> str:
    return f"{abs(value) * 100:.{digits}f}%"


def _signed_pct(value, digits: int = 1) -> str:
    return f"{value * 100:+.{digits}f}%"


def _n(value, digits: int = 2) -> str:
    return f"{value:.{digits}f}"


def _when(value, year: bool = False) -> str:
    stamp = pd.Timestamp(value)
    if year:
        return f"{stamp.day} {stamp:%b %Y}"
    return f"{stamp.day} {stamp:%b}"


def _row(frame: pd.DataFrame, group: str, asset: str) -> pd.Series:
    hit = frame[(frame["group"] == group) & (frame["asset"] == asset)]
    return hit.iloc[0]


def _episode_row(stats: pd.DataFrame, episode_id: str, asset: str) -> pd.Series:
    hit = stats[(stats["id"] == episode_id) & (stats["asset"] == asset)]
    return hit.iloc[0]


def render_deck_notes(
    paired: pd.DataFrame,
    metrics_all: pd.DataFrame,
    metrics_vol: pd.DataFrame,
    metrics_credit: pd.DataFrame,
    metrics_slope: pd.DataFrame,
    stability: pd.DataFrame,
    episode_stats: pd.DataFrame,
    episode_context: pd.DataFrame,
    hmm: dict,
    reading: pd.Series,
) -> str:
    """Slide-ready paragraphs. Every figure is taken from the tables written next to this file."""
    def m(group, asset, frame=metrics_vol):
        return _row(frame, group, asset)

    def s(group, asset):
        return _row(stability, group, asset)

    def e(episode_id, asset):
        return _episode_row(episode_stats, episode_id, asset)

    ctx = episode_context.set_index("id")
    overall = {asset: _row(metrics_all, "All", asset) for asset in ASSETS}
    halves = sample_halves(paired.index)
    calm_wide = paired[(paired["vol_regime"] == "Low/Falling") & (paired["credit_signal"] == "Widening")]
    wide_means = {}
    for label in ("First half", "Second half"):
        mask = halves.reindex(calm_wide.index) == label
        wide_means[label] = (float(calm_wide.loc[mask, BENCHMARK].mean()), int(mask.sum()))
    state_vix = hmm["frame"].groupby("hmm_state")["vix"].mean()
    high_rising = hmm["frame"]["vol_regime"] == "High/Rising"
    share_stress = float((hmm["frame"].loc[high_rising, "hmm_state"] == "Stress").mean())
    low_falling = hmm["frame"]["vol_regime"] == "Low/Falling"
    share_calm = float((hmm["frame"].loc[low_falling, "hmm_state"] == "Calm").mean())
    as_of = pd.Timestamp(reading["as_of"])
    anchor = pd.Timestamp(reading["anchor_1m"])

    spx, pput, cll, put = overall["SPX_TR"], overall["PPUT"], overall["CLL"], overall["PUT"]
    bxm, bxmd, bfly, cndr = overall["BXM"], overall["BXMD"], overall["BFLY"], overall["CNDR"]
    calm, wake, stress, recov = (m(r, "SPX_TR") for r in VOL_REGIMES)
    paragraphs = []

    def add(title, body):
        paragraphs.append(f"## {title}\n\n{body.strip()}\n")

    add(
        "How the numbers are built",
        f"""The panel is {len(paired)} months, {paired.index.min():%b %Y} to {paired.index.max():%b %Y}. Each return sits next to the regime at the previous month-end. Annualized return is geometric. Sharpe uses the T-bill return known at the start of the month. Max drawdown compounds the months in that bucket in calendar order, so it is the track record of being in the strategy only in those months. Beta is against the S&P 500 total return. Buy-writes and the collar hold the equity. PUT, CNDR and BFLY hold T-bills. The indices have no trading costs, fees or taxes, and the history before each launch date is back-calculated.""",
    )
    add(
        "Full sample",
        f"""The S&P returned {_pct(spx.ann_return)} annualized with a Sharpe of {_n(spx.sharpe)}, a maximum drawdown of {_pct(spx.max_drawdown)} and a 95% CVaR of {_pct(spx.cvar_95)}. PPUT is the closest strategy on Sharpe ({_n(pput.sharpe)}), with a drawdown of {_pct(pput.max_drawdown)}, a CVaR of {_pct(pput.cvar_95)} and a beta of {_n(pput.beta)}. CLL has a drawdown of {_pct(cll.max_drawdown)} and a CVaR of {_pct(cll.cvar_95)} at a Sharpe of {_n(cll.sharpe)} and a beta of {_n(cll.beta)}. The buy-writes cut beta (BXM {_n(bxm.beta)}, BXMD {_n(bxmd.beta)}) and lag the market's Sharpe (BXM {_n(bxm.sharpe)}, BXMD {_n(bxmd.sharpe)}). PUT, which is short an at-the-money put against T-bills, has a Sharpe of {_n(put.sharpe)} and a beta of {_n(put.beta)}. CNDR's Sharpe is {_n(cndr.sharpe)} with a beta of {_n(cndr.beta)}. BFLY lost {_pct(bfly.ann_return)} annualized, Sharpe {_n(bfly.sharpe)}, and its stitched drawdown of {_pct(bfly.max_drawdown)} is deeper than the S&P's, on a beta of {_n(bfly.beta)}. That drawdown is the accumulated bleed in ordinary months.""",
    )
    c_put, c_bxm, c_pput, c_cll, c_bfly = (m("Low/Falling", a) for a in ("PUT", "BXM", "PPUT", "CLL", "BFLY"))
    c_put_h, c_spx_h, c_bfly_h = (s("Low/Falling", a) for a in ("PUT", "SPX_TR", "BFLY"))
    add(
        "Low/Falling, calm (85 months)",
        f"""Income is the book that earns its keep when VIX is below its trailing median and falling. PUT returned {_pct(c_put.ann_return)} with a Sharpe of {_n(c_put.sharpe)}, a beta of {_n(c_put.beta)}, a stitched drawdown of {_pct(c_put.max_drawdown)} and a CVaR of {_pct(c_put.cvar_95)}. The S&P in the same months returned {_pct(calm.ann_return)}, Sharpe {_n(calm.sharpe)}, drawdown {_pct(calm.max_drawdown)}, CVaR {_pct(calm.cvar_95)}. BXM's Sharpe is {_n(c_bxm.sharpe)} with a beta of {_n(c_bxm.beta)}; BXMD and BXY are in the same range and carry more equity. PPUT's Sharpe is {_n(c_pput.sharpe)} and CLL's is {_n(c_cll.sharpe)}: the premium spent on protection is visible in the average. Both halves agree on the sign for PUT (Sharpe {_n(c_put_h.sharpe_first)} then {_n(c_put_h.sharpe_second)}) and for the S&P ({_n(c_spx_h.sharpe_first)} then {_n(c_spx_h.sharpe_second)}). BFLY does not: Sharpe {_n(c_bfly_h.sharpe_first)} in the first half and {_n(c_bfly_h.sharpe_second)} in the second. The worst calm-regime month for the equity books is Sep 2008, which is the month the credit modifier is there to catch. On this record, lean toward PUT and then the buy-writes, and leave the protection books and BFLY off.""",
    )
    w_spx, w_bxmd, w_bxy, w_pput, w_cll, w_put, w_cndr, w_bfly = (
        m("Low/Rising", a) for a in ("SPX_TR", "BXMD", "BXY", "PPUT", "CLL", "PUT", "CNDR", "BFLY")
    )
    w_cndr_h, w_bfly_h = s("Low/Rising", "CNDR"), s("Low/Rising", "BFLY")
    add(
        "Low/Rising, waking up (34 months)",
        f"""The S&P is the strongest book: {_pct(w_spx.ann_return)} annualized, Sharpe {_n(w_spx.sharpe)}. BXMD ({_n(w_bxmd.sharpe)}) and BXY ({_n(w_bxy.sharpe)}) stay close. PPUT ({_n(w_pput.sharpe)}) and CLL ({_n(w_cll.sharpe)}) keep up with the market's Sharpe. PUT falls to {_n(w_put.sharpe)}: an at-the-money put sale lags a market that is still rising. CNDR's Sharpe is {_n(w_cndr.sharpe)} ({_n(w_cndr_h.sharpe_first)} then {_n(w_cndr_h.sharpe_second)} across the halves) and BFLY's is {_n(w_bfly.sharpe)} ({_n(w_bfly_h.sharpe_first)} then {_n(w_bfly_h.sharpe_second)}). The halves have 20 and 14 months, so the ranking among the positive Sharpes moves, while the sign of those Sharpes does not. The usable split is equity or a light out-of-the-money overwrite, and no range sale.""",
    )
    st = {a: m("High/Rising", a) for a in ("SPX_TR", "PPUT", "CLL", "BXM", "BXMD", "BXY", "PUT", "CNDR", "BFLY")}
    st_h = {a: s("High/Rising", a) for a in ("PPUT", "CNDR", "BFLY")}
    hr_w = {a: m("High/Rising | Widening", a, metrics_credit) for a in ("SPX_TR", "PPUT", "PUT")}
    hr_t = {a: m("High/Rising | Tightening", a, metrics_credit) for a in ("SPX_TR", "PUT")}
    add(
        "High/Rising, stress (68 months)",
        f"""Average returns stay positive. The S&P returned {_pct(st['SPX_TR'].ann_return)} with a Sharpe of {_n(st['SPX_TR'].sharpe)}. The reason to change the book is the tail: stitched drawdown {_pct(st['SPX_TR'].max_drawdown)}, CVaR {_pct(st['SPX_TR'].cvar_95)}, worst month {pd.Timestamp(st['SPX_TR'].worst_month):%b %Y} at {_signed_pct(st['SPX_TR'].worst_return)}. PPUT improves the average and the tail together: Sharpe {_n(st['PPUT'].sharpe)}, return {_pct(st['PPUT'].ann_return)}, drawdown {_pct(st['PPUT'].max_drawdown)}, CVaR {_pct(st['PPUT'].cvar_95)}, beta {_n(st['PPUT'].beta)}. The Sharpe is {_n(st_h['PPUT'].sharpe_first)} in the first half and {_n(st_h['PPUT'].sharpe_second)} in the second. CLL cuts the CVaR to {_pct(st['CLL'].cvar_95)} and the drawdown to {_pct(st['CLL'].max_drawdown)}, and the average falls to a Sharpe of {_n(st['CLL'].sharpe)} on a {_pct(st['CLL'].ann_return)} return. Buy-write betas are {_n(st['BXM'].beta)} to {_n(st['BXMD'].beta)}, with drawdowns near {_pct(st['BXMD'].max_drawdown)} and CVaRs near {_pct(st['BXM'].cvar_95)}. PUT's worst month is {pd.Timestamp(st['PUT'].worst_month):%b %Y} at {_signed_pct(st['PUT'].worst_return)}, and its Sharpe is {_n(st['PUT'].sharpe)}. CNDR's Sharpe is {_n(st['CNDR'].sharpe)} ({_n(st_h['CNDR'].sharpe_first)} in the first half, {_n(st_h['CNDR'].sharpe_second)} in the second). BFLY's is {_n(st['BFLY'].sharpe)} ({_n(st_h['BFLY'].sharpe_first)}, then {_n(st_h['BFLY'].sharpe_second)}). No strategy in this regime changes the sign of its excess return between halves. Lean toward PPUT when the aim is return and tail together, and toward CLL when the aim is the worst month. Leave CNDR, BFLY and the put-write.

When the same regime arrives with widening credit ({int(hr_w['SPX_TR'].n)} months), PPUT's Sharpe is {_n(hr_w['PPUT'].sharpe)} and the S&P's CVaR is {_pct(hr_w['SPX_TR'].cvar_95)}. PUT's Sharpe is {_n(hr_w['PUT'].sharpe)} and its drawdown is {_pct(hr_w['PUT'].max_drawdown)}. With tightening credit ({int(hr_t['SPX_TR'].n)} months) PUT's Sharpe is {_n(hr_t['PUT'].sharpe)}, in line with the S&P's {_n(hr_t['SPX_TR'].sharpe)}. The stress book gets stricter while spreads are widening, and income is ordinary again while they are healing.""",
    )
    rc = {a: m("High/Falling", a) for a in ("SPX_TR", "CLL", "PUT", "PPUT", "CNDR", "BFLY")}
    rc_h = {a: s("High/Falling", a) for a in ("SPX_TR", "CLL", "PUT", "CNDR", "BFLY")}
    add(
        "High/Falling, recovery (31 months)",
        f"""This is the smallest volatility bucket: {int(rc_h['SPX_TR'].n_first)} months in the first half and {int(rc_h['SPX_TR'].n_second)} in the second. The S&P returned {_pct(rc['SPX_TR'].ann_return)}, Sharpe {_n(rc['SPX_TR'].sharpe)}, and the Sharpe stays positive in both halves ({_n(rc_h['SPX_TR'].sharpe_first)}, then {_n(rc_h['SPX_TR'].sharpe_second)}). CLL has the highest full-sample Sharpe in the bucket, {_n(rc['CLL'].sharpe)}, with a drawdown of {_pct(rc['CLL'].max_drawdown)} and a beta of {_n(rc['CLL'].beta)}, and it is positive in both halves ({_n(rc_h['CLL'].sharpe_first)}, then {_n(rc_h['CLL'].sharpe_second)}). Its rank still moves from seventh to first, so {_n(rc['CLL'].sharpe)} is a noisy edge. PUT's Sharpe is {_n(rc['PUT'].sharpe)} overall and {_n(rc_h['PUT'].sharpe_first)} then {_n(rc_h['PUT'].sharpe_second)}. CNDR goes from {_n(rc_h['CNDR'].sharpe_first)} to {_n(rc_h['CNDR'].sharpe_second)}, and BFLY from {_n(rc_h['BFLY'].sharpe_first)} to {_n(rc_h['BFLY'].sharpe_second)}. PPUT's Sharpe is {_n(rc['PPUT'].sharpe)}, and its worst month in the bucket is {pd.Timestamp(rc['PPUT'].worst_month):%b %Y} at {_signed_pct(rc['PPUT'].worst_return)}, against the S&P's worst of {_signed_pct(rc['SPX_TR'].worst_return)} in {pd.Timestamp(rc['SPX_TR'].worst_month):%b %Y}. Hold equity beta in a recovery. Do not rank CNDR or BFLY off this cell, and treat a 95% VaR from 31 months as a description of a few bad months.""",
    )
    calm_tight = m("Low/Falling | Tightening", "SPX_TR", metrics_credit)
    calm_wide_m = m("Low/Falling | Widening", "SPX_TR", metrics_credit)
    slope_flat = m("High/Rising | Flattening", "SPX_TR", metrics_slope)
    slope_steep = m("High/Rising | Steepening", "SPX_TR", metrics_slope)
    slope_calm_flat = m("Low/Falling | Flattening", "SPX_TR", metrics_slope)
    slope_calm_steep = m("Low/Falling | Steepening", "SPX_TR", metrics_slope)
    rebound = m("High/Falling | Tightening", "SPX_TR", metrics_credit)
    add(
        "Credit as the added signal",
        f"""Inside Low/Falling, tightening credit ({int(calm_tight.n)} months) lines up with an S&P return of {_pct(calm_tight.ann_return)} and a Sharpe of {_n(calm_tight.sharpe)}. Widening credit ({int(calm_wide_m.n)} months) lines up with an S&P return of {_signed_pct(calm_wide_m.ann_return)} and a Sharpe of {_n(calm_wide_m.sharpe)}. That widening cell is the reading in front of both Sep 2008 and Jan 2022. It does not repeat at the same size: the S&P averaged {_signed_pct(wide_means['First half'][0], 1)} a month in the first half of the sample ({wide_means['First half'][1]} months) and {_signed_pct(wide_means['Second half'][0], 1)} a month in the second ({wide_means['Second half'][1]} months). Use it as a reason to cut equity-collateral income when volatility looks calm and spreads are widening, and keep the confidence low. Low/Rising with widening spreads is 5 months and is set aside. High/Falling with tightening spreads is {int(rebound.n)} months and an S&P return of {_pct(rebound.ann_return)}, which is a small-sample rebound.

The 2s5s slope leaves High/Rising equity returns almost unchanged ({_pct(slope_flat.ann_return)} when the curve is flattening, {_pct(slope_steep.ann_return)} when it is steepening). It does separate Low/Falling ({_pct(slope_calm_steep.ann_return)} steepening versus {_pct(slope_calm_flat.ann_return)} flattening). Credit is the added signal because it is the one that changes the stress decision, which is where the strategy ranking moves.""",
    )

    def episode_returns(episode_id):
        bits = [f"{asset} {_signed_pct(e(episode_id, asset).total_return)}" for asset in ASSETS]
        return ", ".join(bits)

    vol = ctx.loc["volmageddon"]
    covid = ctx.loc["covid"]
    bear = ctx.loc["bear_2022"]
    gfc = ctx.loc["gfc"]
    add(
        "Volmageddon, 26 Jan to 8 Feb 2018",
        f"""The S&P fell {_pct(e('volmageddon', 'SPX_TR').total_return)} and VIX reached {vol.vix_max:.1f} on {_when(vol.vix_max_date)}. Every strategy lost, and the range was tight: {episode_returns('volmageddon')}. The defined-risk books lost less. A 5% out-of-the-money put cushioned the index only partly, over a two-week window.

On {_when(vol.prior_date, year=True)} the reading was {vol.prior_vol_regime}, VIX {vol.prior_vix:.1f} against a trailing median of {vol.prior_vix_median:.1f}, credit {str(vol.prior_credit).lower()}. A VIX of {vol.prior_vix:.1f} is a low level. The High label records that it sat just above that median, and credit was tightening. On {_when(vol.eve_date)} the same rules read {vol.eve_vol_regime}, VIX {vol.eve_vix:.1f}, one-month change {vol.eve_vix_change_1m:+.1f} points. The level was still about 12. The January month-end falls inside the window, so it is concurrent. Filtered HMM P(Stress) on the December month-end was {vol.prior_p_stress:.2f}, state {vol.prior_hmm_state}.""",
    )
    add(
        "COVID crash, 19 Feb to 23 Mar 2020",
        f"""The S&P fell {_pct(e('covid', 'SPX_TR').total_return)}. VIX peaked at {covid.vix_max:.1f} on {_when(covid.vix_max_date)}. Buy-writes and the put-write fell with the equity: {episode_returns('covid')}. CLL, PPUT, CNDR and BFLY are the books whose wings or outright put were built for this path.

On {_when(covid.prior_date, year=True)} the reading was already {covid.prior_vol_regime}, VIX {covid.prior_vix:.1f} against a median of {covid.prior_vix_median:.1f}. Credit was {str(covid.prior_credit).lower()}, so the volatility rule had flipped and the credit modifier had not. On {_when(covid.eve_date)}, VIX was {covid.eve_vix:.1f} and the same rules read {covid.eve_vol_regime}, one-month change {covid.eve_vix_change_1m:+.1f} points. The January month-end was the warning, and by the eve it had faded. Filtered HMM P(Stress) on 31 Jan was {covid.prior_p_stress:.2f}, state {covid.prior_hmm_state}. The HMM moved to Stress in February, which is inside the window.""",
    )
    add(
        "2022 bear market, 3 Jan to 12 Oct",
        f"""The S&P fell {_pct(e('bear_2022', 'SPX_TR').total_return)} and VIX reached {bear.vix_max:.1f} on {_when(bear.vix_max_date)}. The path was a grind. PPUT fell {_pct(e('bear_2022', 'PPUT').total_return)}, close to the equity, because a one-month 5% put rolled through a slow decline still leaves the strategy long the S&P. CLL fell {_pct(e('bear_2022', 'CLL').total_return)}, the buy-writes between {_pct(e('bear_2022', 'BXM').total_return)} and {_pct(e('bear_2022', 'BXMD').total_return)}, PUT {_pct(e('bear_2022', 'PUT').total_return)}, CNDR {_pct(e('bear_2022', 'CNDR').total_return)} and BFLY {_pct(e('bear_2022', 'BFLY').total_return)}. The defined-risk books held up. The rolling crash hedge did not. Full path: {episode_returns('bear_2022')}.

On {_when(bear.prior_date, year=True)} the reading was {bear.prior_vol_regime}: VIX {bear.prior_vix:.1f} was below its median of {bear.prior_vix_median:.1f}, and credit was {str(bear.prior_credit).lower()}. The volatility grid was quiet. Credit was the signal that had flipped. The window starts on the first session of January, so the eve reading is that same month-end. Filtered HMM P(Stress) was {bear.prior_p_stress:.2f}, state {bear.prior_hmm_state}.""",
    )
    add(
        "Financial crisis, 15 Sep 2008 to 9 Mar 2009",
        f"""This is the deep bear the other three windows are not, and the spread across strategies is about 40 percentage points. The S&P fell {_pct(e('gfc', 'SPX_TR').total_return)}, the drawdown inside the window is {_pct(e('gfc', 'SPX_TR').max_drawdown)}, and VIX reached {gfc.vix_max:.1f} on {_when(gfc.vix_max_date)}. CNDR fell {_pct(e('gfc', 'CNDR').total_return)} and BFLY {_pct(e('gfc', 'BFLY').total_return)}. PUT fell {_pct(e('gfc', 'PUT').total_return)}: the same T-bill collateral as the condor, short an uncapped crash. Buy-writes fell {_pct(e('gfc', 'BXM').total_return)} to {_pct(e('gfc', 'BXMD').total_return)}. CLL fell {_pct(e('gfc', 'CLL').total_return)} and PPUT {_pct(e('gfc', 'PPUT').total_return)}. Both hold the S&P, so the put reduced the loss and a large equity loss remained. The condor's wings are the difference between CNDR and PUT. Full path: {episode_returns('gfc')}.

On {_when(gfc.prior_date, year=True)} the reading was {gfc.prior_vol_regime}, VIX {gfc.prior_vix:.1f} below a median of {gfc.prior_vix_median:.1f}, credit {str(gfc.prior_credit).lower()}. The relative volatility rule was quiet because the median had already risen, and credit had widened. On {_when(gfc.eve_date)}, before the window opens, the same rules read {gfc.eve_vol_regime}, VIX {gfc.eve_vix:.1f}, up {gfc.eve_vix_change_1m:.1f} points from the August month-end, credit still {str(gfc.eve_credit).lower()}. A check between month-ends would have flipped. A process that waits for month-end would have carried Low/Falling into the week of 15 Sep. Filtered HMM P(Stress) on 29 Aug is {gfc.prior_p_stress:.2f}, and that date is the first month of the HMM sample. The estimated initial distribution is entirely Stress, so the August probability is the starting state, not a warning.""",
    )
    add(
        "What the hidden Markov model adds",
        f"""The model has three Gaussian states on log VIX, the one-month VIX change and the three-month credit change, standardized on the full sample. Ordered by average VIX they are Calm ({state_vix['Calm']:.0f}), Transition ({state_vix['Transition']:.0f}) and Stress ({state_vix['Stress']:.0f}). The chance of staying in a state is high (the diagonal of the transition matrix is about 0.87 to 0.92). The Viterbi labels use the whole sample. The probabilities in the episodes above are filtered, so each one uses only data through that month, apart from the opening month noted there.

These states are a level of volatility. They are not the 2x2 grid. {share_stress:.0%} of High/Rising months fall in HMM Stress, and {share_calm:.0%} of Low/Falling months fall in HMM Calm. A VIX near 11 can be High on the rule, because the rule compares VIX with the last year, and Calm in the HMM, because 11 is a low level. That is the December 2017 label. Filtered P(Stress) was about zero on the month-end before Volmageddon, COVID and the 2022 window. On 31 Jan 2020 the rule was already High/Rising and the HMM was still Calm. The level model is a check on what High means. It did not turn earlier than the rules.""",
    )
    add(
        f"Monitor, {_when(as_of, year=True)}",
        f"""Latest saved close, not a live quote. VIX {float(reading['vix']):.2f} against a trailing median of {float(reading['vix_median_12m']):.2f}, change {float(reading['vix_change_1m']):+.2f} since {_when(anchor)}. {reading['vol_regime']}, credit {str(reading['credit_signal']).lower()}, 2s5s {str(reading['slope_signal']).lower()}. The October month-end had the same volatility and credit reading. The history for the slide is the High/Rising block above, and the stricter cut is High/Rising with widening credit ({int(hr_w['SPX_TR'].n)} months): PPUT Sharpe {_n(hr_w['PPUT'].sharpe)}, PUT Sharpe {_n(hr_w['PUT'].sharpe)}.""",
    )
    header = (
        "# Deck notes\n\n"
        "Paste-ready paragraphs for the dry run. Figures match `ip3_compiled.xlsx`. "
        "Sharpe is excess over T-bills. Drawdowns and CVaRs are losses. "
        "A positive signed return is a gain.\n"
    )
    return header + "\n" + "\n".join(paragraphs)


def _year_axis(ax) -> None:
    import matplotlib.dates as mdates

    ax.xaxis.set_major_locator(mdates.YearLocator(2))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax.tick_params(axis="x", labelrotation=0)


def _strategy_legend(fig, ncol: int = 5) -> None:
    import matplotlib.pyplot as plt

    handles = [
        plt.Line2D([0], [0], color=STRATEGY_COLORS[asset], lw=2.2, label=asset)
        for asset in ASSETS
    ]
    fig.legend(
        handles=handles, loc="upper center", bbox_to_anchor=(0.5, 0.02),
        ncol=ncol, frameon=False, fontsize=9,
    )


def _leader(paired: pd.DataFrame, columns: list[str], window: int = 12) -> pd.Series:
    """Strategy with the highest trailing total return. Ties go to the first name in `columns`."""
    growth = (1 + paired[columns]).rolling(window).apply(np.prod, raw=True) - 1
    ready = growth.notna().all(axis=1)
    leader = pd.Series(index=growth.index, dtype=object)
    leader.loc[ready] = growth.loc[ready].idxmax(axis=1)
    return leader


def _spans(labels: pd.Series) -> list[tuple]:
    """Consecutive months with the same label, as (start, end, label)."""
    clean = labels.dropna()
    if clean.empty:
        return []
    runs = []
    start = clean.index[0]
    current = clean.iloc[0]
    prev = clean.index[0]
    for stamp, label in clean.iloc[1:].items():
        if label != current:
            runs.append((start, prev, current))
            start, current = stamp, label
        prev = stamp
    runs.append((start, prev, current))
    return runs


def _draw_leader_strip(ax, labels: pd.Series, ylabel: str) -> None:
    import matplotlib.dates as mdates

    for start, end, label in _spans(labels):
        left = (start.to_period("M") - 1).to_timestamp(how="end")
        ax.barh(
            0, mdates.date2num(end) - mdates.date2num(left), left=mdates.date2num(left),
            height=0.8, color=STRATEGY_COLORS[label], align="center",
        )
    ax.set_yticks([0])
    ax.set_yticklabels([ylabel])
    ax.set_ylim(-0.6, 0.6)
    ax.xaxis_date()
    ax.tick_params(axis="y", length=0)
    for spine in ax.spines.values():
        spine.set_visible(False)


def _finish(fig, path: Path, note: str) -> None:
    if note:
        fig.text(0.5, -0.02, note, ha="center", va="top", fontsize=8, color="#6B645C")
    fig.savefig(path, dpi=160, bbox_inches="tight", facecolor="white")
    import matplotlib.pyplot as plt
    plt.close(fig)


def save_deck_charts(
    paired: pd.DataFrame, episode_stats: pd.DataFrame | None = None, hmm: dict | None = None
) -> list[Path]:
    """Slide-sized charts for Steps 3 and 4. Files land in output/."""
    import matplotlib.pyplot as plt

    OUTPUT.mkdir(parents=True, exist_ok=True)
    saved = []

    growth = (1 + paired[ASSETS]).cumprod()
    relative = growth.div(growth[BENCHMARK], axis=0) * 100

    fig, ax = plt.subplots(figsize=(13.2, 6.4))
    for asset in ASSETS:
        ax.plot(
            growth.index, growth[asset], color=STRATEGY_COLORS[asset],
            lw=2.4 if asset == BENCHMARK else 1.35, zorder=3,
        )
    ax.set_title("Growth of $1 since Sep 2008")
    ax.set_ylabel("Wealth")
    _year_axis(ax)
    _strategy_legend(fig)
    fig.tight_layout(rect=(0, 0.10, 1, 1))
    path = OUTPUT / "deck_growth.png"
    _finish(fig, path, "")
    saved.append(path)

    fig, ax = plt.subplots(figsize=(13.2, 6.4))
    ax.axhline(100, color="#1C1915", lw=0.7, zorder=2)
    for asset in ASSETS:
        if asset == BENCHMARK:
            continue
        ax.plot(relative.index, relative[asset], color=STRATEGY_COLORS[asset], lw=1.45, zorder=3)
    ax.set_title("Wealth relative to the S&P 500. 100 means it kept pace")
    ax.set_ylabel("Relative wealth")
    _year_axis(ax)
    handles = [
        plt.Line2D([0], [0], color=STRATEGY_COLORS[asset], lw=2.2, label=asset)
        for asset in ASSETS if asset != BENCHMARK
    ]
    fig.legend(
        handles=handles, loc="upper center", bbox_to_anchor=(0.5, 0.02),
        ncol=4, frameon=False, fontsize=9,
    )
    fig.tight_layout(rect=(0, 0.10, 1, 1))
    path = OUTPUT / "deck_vs_spx.png"
    _finish(fig, path, "")
    saved.append(path)

    leaders = _leader(paired, ASSETS)
    strategy_leaders = _leader(paired, list(STRATEGIES))
    fig, axes = plt.subplots(2, 1, figsize=(13.2, 3.6), sharex=True)
    _draw_leader_strip(axes[0], leaders, "Including the S&P")
    _draw_leader_strip(axes[1], strategy_leaders, "Strategies only")
    _year_axis(axes[1])
    axes[0].label_outer()
    axes[0].set_title("Who led over the trailing 12 months")
    _strategy_legend(fig, ncol=9)
    fig.tight_layout(rect=(0, 0.16, 1, 1))
    path = OUTPUT / "deck_who_led.png"
    _finish(fig, path, "")
    saved.append(path)

    fig, axes = plt.subplots(2, 2, figsize=(13.2, 8.2), sharex=False, sharey=False)
    for ax, regime in zip(axes.ravel(), VOL_REGIMES):
        block = paired.loc[paired["vol_regime"] == regime, ASSETS]
        path_ = (1 + block).cumprod()
        for asset in ASSETS:
            ax.plot(
                range(1, len(path_) + 1), path_[asset], color=STRATEGY_COLORS[asset],
                lw=2.2 if asset == BENCHMARK else 1.25,
            )
        ax.set_title(f"{regime}  ·  {len(block)} months, stitched")
        ax.set_xlabel("Month in this regime")
        ax.set_ylabel("Growth of $1")
    _strategy_legend(fig)
    fig.tight_layout(rect=(0, 0.08, 1, 1))
    path = OUTPUT / "deck_stitched.png"
    _finish(fig, path, "")
    saved.append(path)

    metrics = performance_table(paired, "vol_regime")
    fig, axes = plt.subplots(2, 2, figsize=(13.2, 8.0), sharex=True, sharey=True)
    for ax, regime in zip(axes.ravel(), VOL_REGIMES):
        block = metrics[metrics["group"] == regime].set_index("asset")
        placed = []
        for asset in ASSETS:
            row = block.loc[asset]
            x = float(row["cvar_95"] * 100)
            y = float(row["ann_return"] * 100)
            ax.scatter(x, y, s=70, color=STRATEGY_COLORS[asset], zorder=3)
            dy = 5
            for px, py in placed:
                if abs(x - px) < 1.3 and abs(y - py) < 2.8:
                    dy += 9
            placed.append((x, y))
            ax.annotate(asset, (x, y), textcoords="offset points", xytext=(6, dy), fontsize=8, color="#1C1915")
        ax.axhline(0, color="#bbb", lw=0.6)
        ax.set_title(regime)
        ax.set_xlabel("95% CVaR, loss (%)")
        ax.set_ylabel("Annualized return (%)")
    fig.tight_layout()
    path = OUTPUT / "deck_return_vs_tail.png"
    _finish(fig, path, "Up and to the left is the better combination of average return and tail loss. One point per series.")
    saved.append(path)

    stability = stability_table(paired)
    fig, axes = plt.subplots(2, 2, figsize=(13.2, 8.2), sharey=True)
    for ax, regime in zip(axes.ravel(), VOL_REGIMES):
        block = stability[stability["group"] == regime].set_index("asset").reindex(ASSETS)
        y = range(len(ASSETS))
        for i, asset in enumerate(ASSETS):
            a, b = block.loc[asset, "sharpe_first"], block.loc[asset, "sharpe_second"]
            ax.plot([a, b], [i, i], color="#C8C2B8", lw=1.6, zorder=1)
            ax.scatter([a], [i], s=36, color="#4C78A8", zorder=2)
            ax.scatter([b], [i], s=36, color="#E09F3E", zorder=2)
        ax.axvline(0, color="#888", lw=0.6)
        ax.set_yticks(list(y))
        ax.set_yticklabels(ASSETS)
        ax.set_title(regime)
        ax.set_xlabel("Sharpe")
    axes[0, 0].scatter([], [], color="#4C78A8", label="First half")
    axes[0, 0].scatter([], [], color="#E09F3E", label="Second half")
    axes[0, 0].legend(frameon=False, fontsize=8)
    fig.tight_layout()
    path = OUTPUT / "deck_stability.png"
    _finish(fig, path, "A line that crosses zero changed sign between the two halves of the sample.")
    saved.append(path)

    if episode_stats is not None:
        order = [item["id"] for item in EPISODES]
        names = {item["id"]: item["name"] for item in EPISODES}
        fig, axes = plt.subplots(2, 2, figsize=(13.2, 8.0), sharey=True)
        for ax, episode_id in zip(axes.ravel(), order):
            block = episode_stats[episode_stats["id"] == episode_id].set_index("asset").reindex(ASSETS)
            values = block["total_return"] * 100
            ax.barh(
                ASSETS[::-1], values.iloc[::-1],
                color=[STRATEGY_COLORS[asset] for asset in ASSETS[::-1]],
            )
            ax.axvline(0, color="#1C1915", lw=0.6)
            span = max(abs(values.min()), abs(values.max()))
            ax.set_xlim(-span * 1.25, span * 0.15 if values.max() < 0 else span * 1.25)
            for asset, value in values.items():
                nudge = span * 0.03
                ax.text(
                    value + (nudge if value >= 0 else -nudge), asset,
                    f"{value:.0f}%", va="center",
                    ha="left" if value >= 0 else "right", fontsize=8, color="#1C1915",
                )
            ax.set_title(names[episode_id])
            ax.set_xlabel("Total return (%)")
        fig.tight_layout()
        path = OUTPUT / "deck_episode_bars.png"
        _finish(fig, path, "Price change from the first print in the window to the last. Not a full calendar month.")
        saved.append(path)

    if hmm is not None:
        share = hmm["share"].reindex(index=list(VOL_REGIMES), columns=list(HMM_STATES))
        fig, ax = plt.subplots(figsize=(8.2, 4.6))
        values = share.to_numpy(dtype=float)
        image = ax.imshow(values, cmap="Blues", vmin=0, vmax=0.7, aspect="auto")
        ax.set_xticks(range(len(share.columns)), share.columns)
        ax.set_yticks(range(len(share.index)), share.index)
        for i in range(values.shape[0]):
            for j in range(values.shape[1]):
                ax.text(j, i, f"{values[i, j] * 100:.0f}%", ha="center", va="center", fontsize=11, color="#1C1915")
        ax.set_title("Share of each rule regime in each HMM state")
        fig.colorbar(image, ax=ax, fraction=0.046, pad=0.04)
        fig.tight_layout()
        path = OUTPUT / "deck_hmm_share.png"
        _finish(fig, path, "Rows sum to 100%. HMM states are named by average VIX: Calm, Transition, Stress.")
        saved.append(path)

    return saved


def write_study_outputs(
    paired: pd.DataFrame,
    readings: pd.DataFrame,
    metrics_all: pd.DataFrame,
    metrics_vol: pd.DataFrame,
    metrics_credit: pd.DataFrame,
    metrics_slope: pd.DataFrame,
    stability: pd.DataFrame,
    episode_stats: pd.DataFrame,
    episode_context: pd.DataFrame,
    hmm: dict,
    reading: pd.Series,
    last_month_end: pd.Series,
) -> str:
    """Write the compiled workbook, the Excel monitor and the deck notes. Returns the notes text."""
    OUTPUT.mkdir(parents=True, exist_ok=True)
    history = monitor_history(paired, reading)
    notes = render_deck_notes(
        paired, metrics_all, metrics_vol, metrics_credit, metrics_slope, stability,
        episode_stats, episode_context, hmm, reading,
    )
    (OUTPUT / "deck_notes.md").write_text(notes, encoding="utf-8")
    write_regime_monitor(OUTPUT / "regime_monitor.xlsx", reading, last_month_end, history)
    payload = {
        "notes": workbook_notes(paired, reading, hmm),
        "paired": paired,
        "readings": readings,
        "metrics_all": metrics_all,
        "metrics_vol": metrics_vol,
        "metrics_credit": metrics_credit,
        "metrics_slope": metrics_slope,
        "stability": stability,
        "episode_stats": episode_stats,
        "episode_context": episode_context,
        "hmm": hmm["frame"],
        "hmm_counts": hmm["counts"],
        "hmm_share": hmm["share"],
        "monitor_reading": monitor_snapshot(reading, last_month_end),
        "monitor_history": history,
    }
    write_compiled_workbook(OUTPUT / "ip3_compiled.xlsx", payload)
    save_deck_charts(paired, episode_stats, hmm)
    return notes
