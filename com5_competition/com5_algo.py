"""
RIT COM5 Commodities Capstone -- live competition algo (DMA REST API).

Everything runs at once on one account, decided again every tick:

  EIA prints   A weekly EIA print moves crude by $0.10 per mmbbl of surprise, ramped in over ~30 ticks
               (prints at ticks 90/240/390/540 of each period). The algo reads the print the tick it
               lands, takes the full CRUDE net limit (100) in that direction and goes back to flat 30
               ticks after the print, when the ramp is done.
  Transport    Buys Alaska crude and pipes it to Cushing, and Cushing crude to New York, whenever the
               regional spread beats the live pipeline cost (read from /assets, so news-driven discounts
               are used at once). Each pipeline carries 10 contracts; shipments are staggered (a couple
               per tick) so that one or two storage tanks per location cover the whole flow.
  Refinery     Runs a 3:2:1 batch when the crack at executable prices beats the $300k lease by a margin.
               The crude leg is fixed when the batch starts; only the products move until they are sold.
  Hedge        Crude that is physical (spot and in a pipeline) is hedged with futures so that the CRUDE
               net equals the EIA target. Crude in a pipeline counts against the CRUDE limits; crude in
               the refinery counts against PRODUCT. In period 1 the futures sit in whichever of CL-1F and
               CL-2F is richest against its storage carry.
  Calendar     In period 1, when CL-2F - CL-1F strays far from its 1-month carry, long the cheap future
               and short the rich one with the gross left after the hedge (100 is kept for EIA prints).

Live trading needs only `requests`. The backtest mode replays the same Trader on RIT casefiles through
a simulator of the server's mechanics (needs `openpyxl`); on the practice server it matched live
trading to the dollar over a partial heat and within ~2% over full heats.

Usage:
  python com5_algo.py --check                  # pre-flight: connection, case, limits, assets; no trading
  python com5_algo.py                          # trade heat after heat (joins a heat already running)
  python com5_algo.py --fresh                  # join a heat only at its start
  python com5_algo.py --cases 1                # stop after one heat
  python com5_algo.py --max-seconds 120        # wind down after 2 minutes (practice)
  python com5_algo.py --set THR_AK=0.1         # change one CFG value for this run
  python com5_algo.py --url http://host:port/v1 --user ID --password PW
  python com5_algo.py --backtest case1.xlsx case2.xlsx [--start 2:421]

Each heat writes com5_logs/run_<time>/ (ticks.csv, events.log, summary.json) next to this file.
Ctrl+C winds down: no new trades, crude still in a pipeline is received and sold, then everything is
closed and every lease released. A second Ctrl+C closes what it can at once and exits.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import signal
import time
import traceback
from datetime import datetime

import requests

API_URL = "http://flserver.rotman.utoronto.ca:16615/v1"
API_USER = "1010264928"
API_PASSWORD = "fa"

TICKS = 600
CRUDE = ("CL", "CL-AK", "CL-NYC", "CL-1F", "CL-2F")
PRODUCTS = ("HO", "RB")
TICKERS = CRUDE + PRODUCTS
SPOT = ("CL", "CL-AK", "CL-NYC")
FUTURES = ("CL-1F", "CL-2F")
SIZE = {"CL": 1000, "CL-AK": 1000, "CL-NYC": 1000, "CL-1F": 1000, "CL-2F": 1000, "HO": 42000, "RB": 42000}
TANK = {"CL": "CL-STORAGE", "CL-AK": "AK-STORAGE", "CL-NYC": "NYC-STORAGE"}
ROUTES = {"AK": ("AK-CS-PIPE", "CL-AK", "CL"), "NYC": ("CS-NYC-PIPE", "CL", "CL-NYC")}
PIPE_LOT = 10                    # contracts per pipeline use
TANK_LOT = 10                    # contracts per storage tank
MAX_LEASES = 10                  # per storage type and per pipeline
MAX_ORDER = 30
NET_LIMIT, GROSS_LIMIT = 100, 500
FEE = 1.0
TANK_COST = 500.0
REFINERY = "CL-REFINERY"
REFINERY_COST = 300_000.0
REFINERY_IN = 30                 # CL contracts per batch -> 10 HO + 20 RB
PIPE_TICKS, REFINERY_TICKS = 30, 45

EIA_RE = re.compile(r"WEEK\s*(\d+).*?ACTUAL\s+(DRAW|BUILD)\s+(?:OF\s+)?(\d+(?:\.\d+)?)\s*(?:MLN|MILLION|MM)?\s*"
                    r"(?:BBLS?|BARRELS)?.*?FORECAST\s+(DRAW|BUILD)\s+(?:OF\s+)?(\d+(?:\.\d+)?)", re.I | re.S)

CFG = {
    "EIA_SIZE": 100,         # CRUDE net taken on a print
    "EIA_HOLD": 30,          # ticks after the print tick; the ramp is ~89% done at +30 and flat after
    "EIA_PER_MMBBL": 0.10,   # $/bbl per mmbbl of surprise
    "EIA_RAMP_SHARE": 0.89,
    "EIA_MIN_EDGE": 0.08,    # $/bbl of ramp still to come; smaller (or late) prints are skipped
    "THR_AK": 0.05,          # $/bbl over every cost, AK -> Cushing
    "THR_NYC": 0.10,         # $/bbl over every cost, Cushing -> NYC with bought crude
    "THR_FWD": 0.00,         # $/bbl, piping arrived Cushing crude on to NYC instead of selling it
    "SHIP_PER_TICK": 2,      # pipelines started per route per tick (arrivals per tick -> tanks needed)
    "STORE_BBL": 0.02,       # $/bbl storage per location, amortized over the staggered flow
    "HEDGE_BBL": 0.01,       # $/bbl expected hedge cost per shipment
    "MU_NYC": 2.0,           # long-run NYC - CL, $/bbl
    "HL_NYC": 84.0,          # half-life of NYC - CL, ticks
    "PRIOR_W": 300.0,        # weight (ticks) of MU_NYC against this case's running mean
    "REF_THR": 40_000.0,     # $ per batch over cost at executable prices
    "HEDGE_BAND": 10,        # contracts of CRUDE net drift allowed outside EIA windows
    "EIA_BAND": 3,
    "USE_1F": 1,             # 1: in period 1 the hedge may sit in CL-1F
    "PLACE_SWITCH": 0.12,    # $/bbl edge difference to move the futures book between 1F and 2F
    "P1_ROLL": 585,          # period-1 tick from which only CL-2F is used
    "CAL_SIZE": 100,         # period-1 calendar (long the cheap future, short the rich one), per leg
    "CAL_OPEN": 0.40,        # $/bbl of (2F - 1F - 1.0) to open the calendar
    "CAL_STEP": 20,          # contracts; smaller calendar changes are not traded
    "END_BUFFER": 4,         # pipes and refinery must finish this many ticks before the period ends
    "FLAT_AT": 596,          # period-2 tick at which everything is closed
}


# ------------------------------------------------------------------------------ helpers
def kk(period, tick):
    return (int(period) - 1) * TICKS + int(tick)


def finish_k(lease):
    p, t = lease.get("convert_finish_period"), lease.get("convert_finish_tick")
    return None if p is None or t is None else kk(p, t)


def next_lease_k(lease):
    p, t = lease.get("next_lease_period"), lease.get("next_lease_tick")
    return None if p is None or t is None else kk(p, t)


def theo(fut, period, tick):
    """Storage carry of a future over spot, $/bbl ($500/day per 10k bbl, 20 days per period)."""
    rem = max(0.0, 1.0 - tick / TICKS)
    if fut == "CL-1F":
        return rem
    return 1.0 + rem if period == 1 else rem


def eia_move(text, per_mmbbl):
    m = EIA_RE.search(text or "")
    if not m:
        return None
    week, a_dir, a_amt, f_dir, f_amt = m.groups()
    actual = float(a_amt) * (1 if a_dir.upper() == "BUILD" else -1)
    forecast = float(f_amt) * (1 if f_dir.upper() == "BUILD" else -1)
    return int(week), round(-per_mmbbl * (actual - forecast), 4)


# ------------------------------------------------------------------------------ live exchange
class LiveExchange:
    """The RIT DMA REST API. Every method returns plain data; errors come back as values."""

    def __init__(self, url, user, password):
        self.url = url.rstrip("/")
        self.s = requests.Session()
        self.s.auth = (user, password)

    def _call(self, method, path, params=None):
        for _ in range(50):
            try:
                r = self.s.request(method, self.url + path, params=params, timeout=3)
            except requests.RequestException as exc:
                return None, f"{exc.__class__.__name__}"
            if r.status_code == 429:
                try:
                    time.sleep(min(max(float(r.json().get("wait", 0.01)), 0.002), 0.5))
                except (ValueError, AttributeError):
                    time.sleep(0.01)
                continue
            try:
                body = r.json()
            except ValueError:
                body = r.text
            if r.ok:
                return body, ""
            msg = body.get("message") if isinstance(body, dict) else str(body)
            return None, f"{r.status_code} {str(msg)[:160]}"
        return None, "rate limit"

    def get(self, path, **params):
        body, _ = self._call("GET", path, params or None)
        return body

    def case(self):
        return self.get("/case")

    def securities(self):
        return self.get("/securities")

    def leases(self):
        return self.get("/leases")

    def assets(self):
        return self.get("/assets")

    def news(self, after):
        return self.get("/news", after=after, limit=100)

    def trader(self):
        return self.get("/trader")

    def limits(self):
        return self.get("/limits")

    def order(self, ticker, action, qty):
        body, err = self._call("POST", "/orders", {"ticker": ticker, "type": "MARKET", "quantity": int(qty),
                                                    "action": action})
        if body is None:
            return 0, None, err
        return int(round(float(body.get("quantity_filled") or 0))), body.get("vwap"), ""

    def lease(self, ticker, **params):
        return self._call("POST", "/leases", {"ticker": ticker, **params})

    def use(self, lease_id, **params):
        return self._call("POST", f"/leases/{lease_id}", params)

    def release(self, lease_id):
        body, err = self._call("DELETE", f"/leases/{lease_id}")
        return err == "", err


# ------------------------------------------------------------------------------ logging
class Log:
    def __init__(self, root, quiet=False):
        self.folder = os.path.join(root, datetime.now().strftime("run_%Y%m%d_%H%M%S"))
        os.makedirs(self.folder, exist_ok=True)
        self.events = open(os.path.join(self.folder, "events.log"), "w", encoding="utf-8")
        self.tick_file = open(os.path.join(self.folder, "ticks.csv"), "w", newline="")
        self.tick_writer = None
        self.quiet = quiet

    def event(self, k, text):
        line = f"[P{(k - 1) // TICKS + 1} t{(k - 1) % TICKS + 1:3d}] {text}"
        self.events.write(line + "\n")
        self.events.flush()
        if not self.quiet:
            print(line, flush=True)

    def tick(self, row):
        if self.tick_writer is None:
            self.tick_writer = csv.DictWriter(self.tick_file, fieldnames=list(row), extrasaction="ignore")
            self.tick_writer.writeheader()
        self.tick_writer.writerow(row)
        self.tick_file.flush()

    def summary(self, data):
        with open(os.path.join(self.folder, "summary.json"), "w") as f:
            json.dump(data, f, indent=2, default=str)

    def close(self):
        self.events.close()
        self.tick_file.close()


class NullLog:
    folder = None
    quiet = True

    def event(self, k, text):
        pass

    def tick(self, row):
        pass

    def summary(self, data):
        pass

    def close(self):
        pass


# ------------------------------------------------------------------------------ trader
class Trader:
    def __init__(self, ex, log, cfg=None):
        self.ex, self.log = ex, log
        self.cfg = dict(CFG, **(cfg or {}))
        self.period = self.tick = self.k = 0
        self.pos = {t: 0 for t in TICKERS}
        self.bid = {t: 0.0 for t in TICKERS}
        self.ask = {t: 0.0 for t in TICKERS}
        self.leases = []
        self.pipe_price = {"AK-CS-PIPE": 40_000.0, "CS-NYC-PIPE": 20_000.0}
        self.assets_k = -99
        self.last_news = 0
        self.weeks = set()
        self.eia = None                      # dict(k0, k1, sign, move, week)
        self.place = "CL-2F"
        self.cal = 0                         # calendar: +n = long n CL-1F / short n CL-2F
        self.nyc_sum = self.nyc_n = 0.0
        self.last_ship = {"AK": -99, "NYC": -99}
        self.shipped = {"AK": 0, "NYC": 0}
        self.nlv = None
        self.fines = 0.0
        self.stats = {"orders": 0, "rejects": 0, "contracts": 0, "ships_AK": 0, "ships_NYC": 0, "forwards": 0,
                      "batches": 0, "prints": 0, "tanks": 0, "errors": 0}
        self.edges = {}
        self.winding = False                 # set on Ctrl+C: no new trades, receive what is in flight
        self.wound = False                   # wind-down finished: flat, no leases
        self.used_refinery = set()
        self.limit = {"CRUDE": (NET_LIMIT, GROSS_LIMIT), "PRODUCT": (NET_LIMIT, GROSS_LIMIT)}
        for row in self.ex.limits() or []:
            name = str(row.get("name", "")).upper()
            if name in self.limit and row.get("net_limit") and row.get("gross_limit"):
                self.limit[name] = (int(row["net_limit"]), int(row["gross_limit"]))
        if self.limit["CRUDE"] != (NET_LIMIT, GROSS_LIMIT):
            self.log.event(0, f"limits from the server: {self.limit}")
        self.cfg["EIA_SIZE"] = min(self.cfg["EIA_SIZE"], self.limit["CRUDE"][0])

    # ---------------------------------------------------------------- market state
    def refresh(self):
        secs = self.ex.securities()
        if secs:
            for s in secs:
                tk = s.get("ticker")
                if tk in self.pos:
                    self.pos[tk] = int(round(float(s.get("position") or 0)))
                    self.bid[tk] = float(s.get("bid") or 0)
                    self.ask[tk] = float(s.get("ask") or 0)
        leases = self.ex.leases()
        if leases is not None:
            self.leases = list(leases)
        if self.k - self.assets_k >= 3:
            self.read_assets()

    def read_assets(self):
        rows = self.ex.assets()
        if rows:
            for a in rows:
                if a.get("ticker") in self.pipe_price and a.get("lease_price") is not None:
                    self.pipe_price[a["ticker"]] = float(a["lease_price"])
            self.assets_k = self.k

    def mid(self, tk):
        return 0.5 * (self.bid[tk] + self.ask[tk])

    def tanks(self, tk):
        return [l for l in self.leases if l.get("ticker") == TANK[tk]]

    def pipes(self, route=None):
        names = [ROUTES[route][0]] if route else [r[0] for r in ROUTES.values()]
        return [l for l in self.leases if l.get("ticker") in names
                and (finish_k(l) is None or finish_k(l) > self.k)]

    def transit(self):
        n = 0
        for l in self.pipes():
            cf = l.get("convert_from") or [{"quantity": PIPE_LOT}]
            n += int(round(float(cf[0].get("quantity") or PIPE_LOT)))
        return n

    def arriving(self, dst, k_from, k_to):
        n = 0
        for route, (pipe, _, d) in ROUTES.items():
            if d != dst:
                continue
            for l in self.pipes(route):
                fk = finish_k(l)
                if fk is not None and k_from < fk <= k_to:
                    n += PIPE_LOT
        return n

    def refinery_lease(self):
        for l in self.leases:
            if l.get("ticker") == REFINERY:
                return l
        return None

    def refinery_content(self):
        l = self.refinery_lease()
        fk = finish_k(l) if l else None
        return REFINERY_IN if fk is not None and fk > self.k else 0

    def usage(self, bucket):
        if bucket == "CRUDE":
            t = self.transit()
            return sum(self.pos[x] for x in CRUDE) + t, sum(abs(self.pos[x]) for x in CRUDE) + t
        c = self.refinery_content()
        return sum(self.pos[x] for x in PRODUCTS) + c, sum(abs(self.pos[x]) for x in PRODUCTS) + c

    def physical(self):
        return self.pos["CL"] + self.pos["CL-AK"] + self.pos["CL-NYC"] + self.transit()

    def end_k(self):
        """Last tick of this period by which pipes and the refinery must be done."""
        return kk(self.period, TICKS) - self.cfg["END_BUFFER"]

    # ---------------------------------------------------------------- execution
    def feasible(self, tk, q):
        if q < 0 and tk not in FUTURES:
            q = max(q, -self.pos[tk])
        if q > 0 and tk in TANK:
            q = min(q, TANK_LOT * len(self.tanks(tk)) - self.pos[tk])
        if q == 0:
            return 0
        bucket = "CRUDE" if tk in CRUDE else "PRODUCT"
        net, gross = self.usage(bucket)
        net_lim, gross_lim = self.limit[bucket]
        p = self.pos[tk]
        while q:
            g2 = gross - abs(p) + abs(p + q)
            if abs(net + q) <= net_lim and (g2 <= gross_lim or g2 <= gross):
                break
            q += -1 if q > 0 else 1
        return q

    def trade(self, tk, q, why=""):
        """Market order(s) for a signed quantity inside every limit. Returns the signed fill."""
        done = 0
        while done != q:
            chunk = max(-MAX_ORDER, min(MAX_ORDER, q - done))
            chunk = self.feasible(tk, chunk)
            if chunk == 0:
                break
            filled, vwap, err = self.ex.order(tk, "BUY" if chunk > 0 else "SELL", abs(chunk))
            self.stats["orders"] += 1
            signed = filled if chunk > 0 else -filled
            self.pos[tk] += signed
            self.stats["contracts"] += filled
            done += signed
            if filled != abs(chunk):
                self.stats["rejects"] += 1
                self.log.event(self.k, f"REJECT {tk} {chunk:+d} filled {filled} {err}")
                break
        if done:
            self.log.event(self.k, f"{why:9s} {tk:6s} {done:+4d}  bid {self.bid[tk]:.2f} ask {self.ask[tk]:.2f}")
        return done

    def make_room(self, delta):
        """Trade futures so that a CRUDE change of `delta` stays inside the net limit."""
        net, _ = self.usage("CRUDE")
        lim = self.limit["CRUDE"][0]
        need = 0
        if net + delta > lim:
            need = lim - (net + delta)
        elif net + delta < -lim:
            need = -lim - (net + delta)
        if not need:
            return
        order = sorted(FUTURES, key=lambda f: (0 if self.pos[f] * need < 0 else 1, f != self.place))
        for f in order:
            if f == "CL-1F" and self.period != 1:
                continue
            need -= self.trade(f, need, "room")
            if not need:
                break

    def sell_physical(self, tk, why):
        q = self.pos[tk]
        if q > 0:
            self.make_room(-q)
            self.trade(tk, -q, why)

    def lease(self, ticker, **params):
        body, err = self.ex.lease(ticker, **params)
        if body is None or not isinstance(body, dict) or "id" not in body:
            self.log.event(self.k, f"LEASE FAIL {ticker} {params} {err}")
            return None
        self.leases.append(body)
        return body

    def release(self, lease, why=""):
        ok, err = self.ex.release(lease["id"])
        if ok:
            self.leases = [l for l in self.leases if l.get("id") != lease["id"]]
            self.log.event(self.k, f"release  {lease['ticker']} #{lease['id']} {why}")
        else:
            self.log.event(self.k, f"RELEASE FAIL {lease['ticker']} #{lease['id']} {err}")
        return ok

    def ensure_space(self, tk, qty):
        """Lease tanks until `qty` more contracts of spot `tk` fit."""
        while TANK_LOT * len(self.tanks(tk)) - self.pos[tk] < qty:
            if len(self.tanks(tk)) >= MAX_LEASES or not self.lease(TANK[tk]):
                return False
            self.stats["tanks"] += 1
        return True

    # ---------------------------------------------------------------- signals
    def target_net(self):
        e = self.eia
        if e and e["k0"] <= self.k < e["k1"] and not self.winding:
            return e["sign"] * self.cfg["EIA_SIZE"]
        return 0

    def compute_edges(self):
        c = self.cfg
        cl_b, cl_a, cl_m = self.bid["CL"], self.ask["CL"], self.mid("CL")
        s_nyc = self.mid("CL-NYC") - cl_m
        self.nyc_sum += s_nyc
        self.nyc_n += 1
        mu = (c["PRIOR_W"] * c["MU_NYC"] + self.nyc_sum) / (c["PRIOR_W"] + self.nyc_n)
        exp_nyc_bid = cl_m + mu + (s_nyc - mu) * 0.5 ** (PIPE_TICKS / c["HL_NYC"]) - 0.01
        pipe_ak = self.pipe_price["AK-CS-PIPE"] / (PIPE_LOT * 1000)
        pipe_nyc = self.pipe_price["CS-NYC-PIPE"] / (PIPE_LOT * 1000)
        fees = 2 * FEE / 1000
        self.edges = {
            "AK": cl_b - self.ask["CL-AK"] - pipe_ak - 2 * c["STORE_BBL"] - fees - c["HEDGE_BBL"],
            "NYC": exp_nyc_bid - cl_a - pipe_nyc - 2 * c["STORE_BBL"] - fees - c["HEDGE_BBL"],
            "FWD": exp_nyc_bid - cl_b - pipe_nyc - c["STORE_BBL"],
            "crack": (10 * self.bid["HO"] + 20 * self.bid["RB"]) * 42000 - REFINERY_IN * 1000 * cl_a,
            "e1": self.mid("CL-1F") - cl_m - theo("CL-1F", self.period, self.tick) if self.period == 1 else 0.0,
            "e2": self.mid("CL-2F") - cl_m - theo("CL-2F", self.period, self.tick),
            "s_nyc": s_nyc, "s_ak": self.mid("CL-AK") - cl_m, "mu_nyc": mu,
        }

    # ---------------------------------------------------------------- news
    def read_news(self):
        items = self.ex.news(self.last_news)
        new_print = False
        for n in sorted(items or [], key=lambda x: x.get("news_id", 0)):
            nid = int(n.get("news_id", 0))
            if nid <= self.last_news:
                continue
            self.last_news = nid
            text = f"{n.get('headline') or ''} {n.get('body') or ''}"
            k_news = kk(n.get("period") or self.period, n.get("tick") or self.tick)
            got = eia_move(n.get("headline") or text, self.cfg["EIA_PER_MMBBL"])
            if got:
                new_print |= self.on_print(k_news, *got)
            elif "PIPELINE" in text.upper():
                self.assets_k = -99
                self.log.event(self.k, f"NEWS {n.get('headline')}")
            elif nid > 1:
                self.log.event(self.k, f"news {n.get('headline')}")
        return new_print

    def on_print(self, k_news, week, move):
        if week in self.weeks:
            return False
        self.weeks.add(week)
        c = self.cfg
        elapsed = max(0, self.k - k_news)
        left = max(0.0, 1.0 - elapsed / c["EIA_HOLD"]) * c["EIA_RAMP_SHARE"] * abs(move)
        if left < c["EIA_MIN_EDGE"]:
            self.log.event(self.k, f"EIA week {week} move {move:+.2f} skipped (ramp left {left:.2f})")
            return False
        self.eia = {"k0": k_news, "k1": k_news + c["EIA_HOLD"], "sign": 1 if move > 0 else -1,
                    "move": move, "week": week}
        self.stats["prints"] += 1
        self.log.event(self.k, f"EIA week {week} move {move:+.2f} -> CRUDE net {self.target_net():+d} "
                               f"until t{(self.eia['k1'] - 1) % TICKS + 1}")
        return True

    # ---------------------------------------------------------------- hedge / EIA exposure
    def placement(self, sign):
        c = self.cfg
        if self.period != 1 or self.tick >= c["P1_ROLL"] or not c["USE_1F"] or sign == 0:
            return "CL-2F"
        e = self.edges
        score = {"CL-1F": -sign * e["e1"], "CL-2F": -sign * e["e2"]}
        best = max(score, key=score.get)
        if best != self.place and score[best] - score[self.place] > c["PLACE_SWITCH"]:
            self.log.event(self.k, f"futures book -> {best} (edge 1F {e['e1']:+.2f} 2F {e['e2']:+.2f})")
            self.place = best
        return self.place

    def calendar(self, phys):
        """Period-1 calendar size: long the cheap future, short the rich one, inside the gross left
        after the hedge and a 100 reserve for the next EIA print."""
        c = self.cfg
        if self.period != 1 or not c["USE_1F"] or self.tick >= c["P1_ROLL"] - 15 or not c["CAL_SIZE"]:
            self.cal = 0
            return 0
        s = self.edges["e2"] - self.edges["e1"]
        net_lim, gross_lim = self.limit["CRUDE"]
        room = max(0, (gross_lim - net_lim - 2 * abs(phys)) // 2)
        want = self.cal
        if want and s * want <= 0:
            want = 0
        if not want and abs(s) >= c["CAL_OPEN"]:
            want = c["CAL_SIZE"] if s > 0 else -c["CAL_SIZE"]
        sign = 1 if want > 0 else -1
        size = min(abs(want), room)
        if size < abs(self.cal) or size - abs(self.cal) >= c["CAL_STEP"] or (want and not self.cal):
            new = sign * size if size >= c["CAL_STEP"] or size < abs(self.cal) else 0
            if new != self.cal:
                self.log.event(self.k, f"calendar {self.cal:+d} -> {new:+d} (2F-1F-1 {s:+.2f})")
                self.cal = new
        return self.cal

    def rebalance(self, extra=0, final=False):
        """Futures so that CRUDE net = target (physical crude + `extra` planned purchases hedged)."""
        D = self.target_net()
        in_window = D != 0
        band = self.cfg["EIA_BAND"] if in_window else self.cfg["HEDGE_BAND"]
        phys = self.physical()
        F = self.pos["CL-1F"] + self.pos["CL-2F"]
        want = D - phys - extra
        lim = self.limit["CRUDE"][0]
        want = max(-lim - phys, min(lim - phys, want))     # never breach the net limit now
        place = self.placement(1 if want > 0 else -1 if want < 0 else 0)
        other = "CL-1F" if place == "CL-2F" else "CL-2F"
        cal = self.calendar(phys)
        targets = {place: want, other: 0}
        targets["CL-1F"] += cal
        targets["CL-2F"] -= cal
        if self.period != 1:
            targets = {"CL-1F": self.pos["CL-1F"], "CL-2F": want}
        diffs = {tk: targets[tk] - self.pos[tk] for tk in FUTURES}
        if abs(want - F) < band and max(abs(d) for d in diffs.values()) < max(band, 10):
            return
        orders = [(tk, d) for tk, d in diffs.items() if d]
        # closing trades first, then the ones that bring the net back toward zero
        net, _ = self.usage("CRUDE")
        orders.sort(key=lambda o: (0 if o[1] * self.pos[o[0]] < 0 else 1, 0 if o[1] * net < 0 else 1))
        for tk, q in orders:
            self.trade(tk, q, "EIA" if in_window else "hedge")

    # ---------------------------------------------------------------- transport
    def can_ship(self, route):
        if self.winding or self.k + PIPE_TICKS > self.end_k():
            return 0
        free = MAX_LEASES - len(self.pipes(route))
        return max(0, min(free, self.cfg["SHIP_PER_TICK"] - self.shipped[route]))

    def ship(self, route, buy=True):
        pipe, src, dst = ROUTES[route]
        if buy:
            if not self.ensure_space(src, PIPE_LOT):
                return False
            self.make_room(PIPE_LOT)
            got = self.trade(src, PIPE_LOT, f"buy>{route}")
            if got < PIPE_LOT:
                if got:
                    self.trade(src, -got, "undo")
                return False
        elif self.pos[src] < PIPE_LOT:
            return False
        lease = self.lease(pipe, from1=src, quantity1=PIPE_LOT)
        if lease is None:
            if buy:
                self.trade(src, -PIPE_LOT, "undo")
            return False
        self.pos[src] -= PIPE_LOT
        self.shipped[route] += 1
        self.last_ship[route] = self.k
        self.stats[f"ships_{route}"] += 1
        if not buy:
            self.stats["forwards"] += 1
        self.log.event(self.k, f"ship {route:3s} {'buy' if buy else 'fwd'}  {pipe} #{lease['id']} "
                               f"edge {self.edges['AK' if route == 'AK' else ('NYC' if buy else 'FWD')]:+.3f} "
                               f"pipe ${self.pipe_price[pipe]:,.0f} arrives t{(finish_k(lease) or 0) - (self.period - 1) * TICKS}")
        return True

    def handle_inventory(self):
        """Sell or forward everything that arrived; sell products."""
        e = self.edges
        self.sell_physical("CL-NYC", "sell NYC")
        if self.pos["CL-AK"] > 0:
            while self.pos["CL-AK"] >= PIPE_LOT and self.can_ship("AK") and e["AK"] >= self.cfg["THR_AK"]:
                if not self.ship("AK", buy=False):
                    break
            self.sell_physical("CL-AK", "sell AK")
        if self.pos["CL"] > 0:
            while self.pos["CL"] >= PIPE_LOT and self.can_ship("NYC") and e["FWD"] >= self.cfg["THR_FWD"]:
                if not self.ship("NYC", buy=False):
                    break
            self.sell_physical("CL", "sell CL")
        for tk in PRODUCTS:
            if self.pos[tk] > 0:
                self.trade(tk, -self.pos[tk], "sell prod")

    def plan(self):
        e, c = self.edges, self.cfg
        n_ak = self.can_ship("AK") if e["AK"] >= c["THR_AK"] else 0
        n_nyc = self.can_ship("NYC") if e["NYC"] >= c["THR_NYC"] else 0
        # gross room for the full hedge at the worst EIA target (-100)
        net_lim, gross_lim = self.limit["CRUDE"]
        room = (gross_lim - net_lim) // 2 - self.physical()
        n_ak = max(0, min(n_ak, room // PIPE_LOT))
        n_nyc = max(0, min(n_nyc, (room - n_ak * PIPE_LOT) // PIPE_LOT))
        return n_ak, n_nyc

    def prepare_storage(self):
        for dst in ("CL", "CL-NYC"):
            need = self.arriving(dst, self.k, self.k + 2)
            if need:
                self.ensure_space(dst, need)

    def release_storage(self, final=False):
        for tk in TANK:
            tanks = sorted(self.tanks(tk), key=lambda l: next_lease_k(l) or 0)
            if not tanks:
                continue
            soon = self.arriving(tk, self.k, self.k + 3)
            keep = math.ceil((max(0, self.pos[tk]) + soon) / TANK_LOT)
            if not final:
                if tk == "CL-AK" and self.k - self.last_ship["AK"] <= 10:
                    keep = max(keep, 1)
                if tk == "CL" and self.k - self.last_ship["NYC"] <= 10:
                    keep = max(keep, 1 + soon // TANK_LOT)
            surplus = len(tanks) - keep
            for l in tanks:
                if surplus <= 0:
                    break
                nk = next_lease_k(l)
                if final or nk is None or nk - self.k <= 2:
                    if self.release(l, "storage"):
                        surplus -= 1

    # ---------------------------------------------------------------- refinery
    def refinery_step(self):
        c = self.cfg
        lease = self.refinery_lease()
        if lease is not None:
            fk = finish_k(lease)
            if fk is None or fk <= self.k:
                self.release(lease, "refinery done" if fk or lease["id"] in self.used_refinery else "refinery unused")
            return
        if self.winding or self.k + REFINERY_TICKS > self.end_k():
            return
        new_tanks = max(0, math.ceil((self.pos["CL"] + REFINERY_IN) / TANK_LOT) - len(self.tanks("CL")))
        profit = self.edges["crack"] - REFINERY_COST - new_tanks * TANK_COST - 3 * REFINERY_IN * FEE
        if profit < c["REF_THR"]:
            return
        self.make_room(REFINERY_IN)
        if not self.ensure_space("CL", REFINERY_IN):
            return
        start_cl = self.pos["CL"]
        got = self.trade("CL", REFINERY_IN, "refinery")
        if got < REFINERY_IN:
            if got:
                self.trade("CL", -got, "undo")
            return
        lease = self.lease(REFINERY)
        if lease is None:
            self.trade("CL", -REFINERY_IN, "undo")
            return
        used, err = self.ex.use(lease["id"], from1="CL", quantity1=REFINERY_IN)
        if not isinstance(used, dict):
            self.log.event(self.k, f"REFINERY USE FAIL {err}")
            self.release(lease, "use failed")
            self.trade("CL", -(self.pos["CL"] - start_cl), "undo")
            return
        lease.update(used)
        self.used_refinery.add(lease["id"])
        self.pos["CL"] -= REFINERY_IN
        self.stats["batches"] += 1
        self.log.event(self.k, f"REFINERY batch #{lease['id']} crack ${self.edges['crack']:,.0f} "
                               f"exp profit ${profit:,.0f} done t{(finish_k(lease) or 0) - (self.period - 1) * TICKS}")

    # ---------------------------------------------------------------- one tick
    def step(self, period, tick):
        self.period, self.tick, self.k = int(period), int(tick), kk(period, tick)
        self.shipped = {"AK": 0, "NYC": 0}
        self.refresh()
        self.read_news()
        self.compute_edges()
        if self.period == 2 and self.tick >= self.cfg["FLAT_AT"]:
            self.flatten()
            self.log_tick()
            return
        self.handle_inventory()
        self.prepare_storage()
        n_ak, n_nyc = self.plan()
        self.rebalance(extra=PIPE_LOT * (n_ak + n_nyc))
        for _ in range(n_ak):
            if not self.ship("AK"):
                break
        for _ in range(n_nyc):
            if not self.can_ship("NYC") or not self.ship("NYC"):
                break
        self.refinery_step()
        self.rebalance()
        self.release_storage()
        if self.winding and not self.transit() and not self.refinery_content():
            self.flatten()
            self.wound = not any(self.pos.values()) and not self.leases
            if self.wound:
                self.log.event(self.k, "wound down: flat, no leases")
        self.log_tick()

    def on_news_fast(self):
        """Between ticks: a print that just landed is traded at once."""
        if self.read_news():
            self.refresh()
            self.compute_edges()
            self.rebalance()

    def flatten(self):
        for tk in FUTURES:
            if self.pos[tk]:
                self.trade(tk, -self.pos[tk], "flatten")
        for tk in SPOT:
            self.sell_physical(tk, "flatten")
        for tk in PRODUCTS:
            if self.pos[tk] > 0:
                self.trade(tk, -self.pos[tk], "flatten")
        lease = self.refinery_lease()
        if lease is not None and (finish_k(lease) is None or finish_k(lease) <= self.k):
            self.release(lease, "flatten")
        self.release_storage(final=True)

    def log_tick(self):
        if self.k % 5 == 0 or self.tick >= TICKS - 1:
            tr = self.ex.trader()
            if tr:
                self.nlv, self.fines = float(tr.get("nlv") or 0), float(tr.get("total_fines") or 0)
        e = self.edges
        net, gross = self.usage("CRUDE")
        row = {"k": self.k, "period": self.period, "tick": self.tick, "CL": round(self.mid("CL"), 3),
               "D": self.target_net(), "net": net, "gross": gross, "phys": self.physical(),
               "F1": self.pos["CL-1F"], "F2": self.pos["CL-2F"],
               "pipes_AK": len(self.pipes("AK")), "pipes_NYC": len(self.pipes("NYC")),
               "tanks": "/".join(str(len(self.tanks(t))) for t in TANK),
               "e_ak": round(e.get("AK", 0), 3), "e_nyc": round(e.get("NYC", 0), 3), "e_fwd": round(e.get("FWD", 0), 3),
               "crack": round(e.get("crack", 0)), "e1": round(e.get("e1", 0), 3), "e2": round(e.get("e2", 0), 3),
               "pipe_ak": self.pipe_price["AK-CS-PIPE"], "pipe_nyc": self.pipe_price["CS-NYC-PIPE"],
               "refinery": int(self.refinery_lease() is not None), "nlv": self.nlv, "fines": self.fines}
        self.log.tick(row)
        if self.k % 10 == 0 and not getattr(self.log, "quiet", True):
            print(f"P{self.period} t{self.tick:3d} CL {row['CL']:7.2f} D {row['D']:+4d} net {net:+4d} gross {gross:3d} "
                  f"F1 {row['F1']:+4d} F2 {row['F2']:+4d} pipes {row['pipes_AK']:2d}/{row['pipes_NYC']:2d} "
                  f"tanks {row['tanks']} eAK {row['e_ak']:+.2f} eNYC {row['e_nyc']:+.2f} "
                  f"crack {row['crack'] / 1000:5.0f}k ref {row['refinery']} nlv {self.nlv or 0:12,.0f}", flush=True)

    def summary(self):
        return {"algo": "com5_algo", "final_nlv": self.nlv, "fines": self.fines, **self.stats, "cfg": self.cfg}


# ------------------------------------------------------------------------------ live loop
stop_requested = False


def _sigint(signum, frame):
    """First Ctrl+C: wind down. Second Ctrl+C: KeyboardInterrupt, close what can be closed at once."""
    global stop_requested
    signal.signal(signal.SIGINT, signal.default_int_handler)
    stop_requested = True
    print("\nCtrl+C: winding down (no new trades; waiting for crude in pipelines). Ctrl+C again to stop now.",
          flush=True)


def wait_for_case(ex, fresh):
    said = False
    while not stop_requested:
        case = ex.case()
        if case and case.get("status") == "ACTIVE" and int(case.get("tick", 0)) >= 1:
            if not fresh or (int(case.get("period", 1)) == 1 and int(case.get("tick", 0)) <= 3):
                return case
        if not said:
            print(f"connected to {ex.url}; waiting for a case to start" if case else f"NO CONNECTION to {ex.url}",
                  flush=True)
            said = True
        time.sleep(0.05)
    return None


def run_heat(ex, cfg, log_root, max_seconds=0.0):
    global TICKS
    case = ex.case() or {}
    TICKS = int(case.get("ticks_per_period") or TICKS)
    log = Log(log_root)
    trader = Trader(ex, log, cfg)
    last = None
    t_start = time.time()
    print(f"logging to {log.folder}", flush=True)
    try:
        while not trader.wound:
            if (stop_requested or (max_seconds and time.time() - t_start > max_seconds)) and not trader.winding:
                trader.winding = True
                log.event(trader.k, "winding down")
            try:
                case = ex.case()
                if not case:
                    time.sleep(0.05)
                    continue
                key = (int(case.get("period", 0)), int(case.get("tick", 0)))
                if case.get("status") != "ACTIVE":
                    if last is not None and case.get("status") == "STOPPED":
                        break
                    time.sleep(0.1)                     # paused: keep everything, wait
                    continue
                if key[1] < 1:                          # trading opens at tick 1
                    time.sleep(0.02)
                    continue
                if last is not None and key < last:
                    break                               # a new heat started
                if key != last:
                    last = key
                    trader.step(*key)
                    if key[0] == 2 and key[1] >= TICKS:
                        break
                else:
                    trader.on_news_fast()
                    time.sleep(0.02)
            except KeyboardInterrupt:
                raise
            except Exception:
                trader.stats["errors"] += 1
                traceback.print_exc()
                time.sleep(0.1)
    except KeyboardInterrupt:
        print("stopping now: closing positions and leases", flush=True)
        try:
            trader.refresh()
            trader.compute_edges()
            trader.flatten()
        except Exception:
            traceback.print_exc()
    time.sleep(1.0)
    tr = ex.trader()
    if tr:
        trader.nlv, trader.fines = float(tr.get("nlv") or 0), float(tr.get("total_fines") or 0)
    s = trader.summary()
    log.summary(s)
    print("CASE DONE", {k: v for k, v in s.items() if k != "cfg"}, flush=True)
    log.close()
    return s


def preflight(ex):
    """Connection and case check without trading. Returns True when everything needed answers."""
    ok = True
    t0 = time.perf_counter()
    me = ex.trader()
    ms = (time.perf_counter() - t0) * 1000
    if not me:
        print(f"FAIL  no answer from {ex.url} (check --url, --user, --password and that the case is loaded)")
        return False
    print(f"ok    trader {me.get('trader_id')} ({me.get('first_name', '')} {me.get('last_name', '')}), "
          f"NLV {float(me.get('nlv') or 0):,.0f}, fines {float(me.get('total_fines') or 0):,.0f}, {ms:.0f} ms")
    case = ex.case() or {}
    print(f"ok    case '{case.get('name')}' period {case.get('period')} tick {case.get('tick')} "
          f"of {case.get('ticks_per_period')} status {case.get('status')}")
    if case.get("ticks_per_period") and int(case["ticks_per_period"]) != 600:
        print(f"note  {case['ticks_per_period']} ticks per period (the brief says 600); the algo adapts")
    secs = {s.get("ticker"): s for s in ex.securities() or []}
    missing = [t for t in TICKERS if t not in secs]
    if missing:
        ok = False
        print(f"FAIL  securities missing: {missing}")
    else:
        print("ok    securities " + " ".join(f"{t} {secs[t].get('bid')}/{secs[t].get('ask')}" for t in TICKERS))
    names = {a.get("ticker"): a for a in ex.assets() or []}
    need = list(TANK.values()) + [r[0] for r in ROUTES.values()] + [REFINERY]
    if any(n not in names for n in need):
        ok = False
        print(f"FAIL  assets missing: {[n for n in need if n not in names]}")
    else:
        print("ok    assets " + " ".join(f"{n} ${float(names[n].get('lease_price') or 0):,.0f}" for n in need))
    for row in ex.limits() or []:
        print(f"ok    limit {row.get('name')}: net {row.get('net_limit')} gross {row.get('gross_limit')}")
    held = {t: s.get("position") for t, s in secs.items() if s.get("position")}
    leases = ex.leases() or []
    if held or leases:
        print(f"note  already holding {held} with {len(leases)} leases; the algo takes them over")
    print("READY" if ok else "NOT READY")
    return ok


# ------------------------------------------------------------------------------ backtest
class SimExchange:
    """The RIT server's mechanics as the algo meets them, driven by a casefile's price paths.

    Checked on the practice server: quotes are the path rounded to the cent +-0.01 and market orders fill
    there; buying spot crude needs free tank capacity at order time; a pipe debits its input at lease and
    credits the output 30 ticks later, and crude in a pipe counts against CRUDE; the refinery debits 30 CL
    at use and credits 10 HO + 20 RB 45 ticks later, its content counting against PRODUCT; storage costs
    $500 at lease and at every 30-tick renewal, the refinery $300k at lease and at each 60-tick renewal;
    crude arriving beyond tank capacity auto-leases tanks with a $2,000 fine each.
    """

    url = "sim"
    FINE = 2000.0

    def __init__(self, case):
        self.casefile = case
        self.n = 2 * TICKS
        self.k = 1
        self.cash = 0.0
        self.pos = {t: 0 for t in TICKERS}
        self.store = {}
        self.next_id = 1
        self.fines = 0.0
        self.cost = {"fees": 0.0, "storage": 0.0, "pipes": 0.0, "refinery": 0.0, "fines": 0.0}
        self.path = {}
        for tk, col in case["path"].items():
            col = list(col) + [col[-1]] * max(0, self.n - len(col))
            self.path[tk] = [round(x, 2) for x in col[:self.n]]
        self.items = [{"news_id": i + 1, "period": p, "tick": t, "headline": h, "body": b}
                      for i, (p, t, h, b) in enumerate(case["news"])]

    @property
    def period(self):
        return (self.k - 1) // TICKS + 1

    @property
    def tick(self):
        return (self.k - 1) % TICKS + 1

    def mid(self, tk):
        k = self.k if tk != "CL-1F" else min(self.k, TICKS)
        return self.path[tk][k - 1]

    def lease_price(self, pipe):
        return self.casefile["lease"][pipe][self.k - 1]

    def advance(self, k):
        self.k = k
        for lid, l in list(self.store.items()):
            t = l["ticker"]
            if l.get("_finish") == k:
                if t in ("AK-CS-PIPE", "CS-NYC-PIPE"):
                    dst = "CL" if t == "AK-CS-PIPE" else "CL-NYC"
                    self.pos[dst] += l["_qty"]
                    del self.store[lid]
                    self.overflow(dst)
                    continue
                if t == REFINERY:
                    self.pos["HO"] += 10
                    self.pos["RB"] += 20
            if l.get("_next") == k:
                fee = REFINERY_COST if t == REFINERY else TANK_COST
                self.cost["refinery" if t == REFINERY else "storage"] += fee
                self.cash -= fee
                l["_next"] += 60 if t == REFINERY else 30
            self.stamp(l)
        if k == TICKS + 1 and self.pos["CL-1F"]:
            q = self.pos["CL-1F"]
            cl = self.path["CL"][TICKS - 1]
            self.cash += q * 1000 * self.path["CL-1F"][TICKS - 1]
            self.pos["CL-1F"] = 0
            if q > 0:
                self.cash -= q * 1000 * cl
                self.pos["CL"] += q
                self.overflow("CL")
            else:
                self.cash += q * 1000 * cl
        if k % 30 == 0:
            for tk in TANK:
                self.overflow(tk)

    def overflow(self, tk):
        cap = TANK_LOT * sum(1 for l in self.store.values() if l["ticker"] == TANK[tk])
        short = self.pos[tk] - cap
        while short > 0:
            self.add_lease(TANK[tk])
            self.fines += self.FINE
            self.cost["fines"] += self.FINE
            self.cash -= self.FINE
            short -= TANK_LOT

    def stamp(self, l):
        for key, field in (("_next", "next_lease"), ("_finish", "convert_finish")):
            v = l.get(key)
            l[f"{field}_period"] = None if v is None else (v - 1) // TICKS + 1
            l[f"{field}_tick"] = None if v is None else (v - 1) % TICKS + 1

    def add_lease(self, ticker, **extra):
        l = {"id": self.next_id, "ticker": ticker, **extra}
        self.next_id += 1
        self.store[l["id"]] = l
        if ticker in TANK.values():
            l["_next"] = self.k + 30
            self.cash -= TANK_COST
            self.cost["storage"] += TANK_COST
        self.stamp(l)
        return l

    def usage(self, bucket):
        if bucket == "CRUDE":
            t = sum(l["_qty"] for l in self.store.values() if l["ticker"] in ("AK-CS-PIPE", "CS-NYC-PIPE"))
            return sum(self.pos[x] for x in CRUDE) + t, sum(abs(self.pos[x]) for x in CRUDE) + t
        c = sum(REFINERY_IN for l in self.store.values() if l["ticker"] == REFINERY
                and l.get("_finish") is not None and l["_finish"] > self.k)
        return sum(self.pos[x] for x in PRODUCTS) + c, sum(abs(self.pos[x]) for x in PRODUCTS) + c

    @staticmethod
    def view(l):
        return {k: v for k, v in l.items() if not k.startswith("_")}

    # the API the Trader calls (same as LiveExchange)
    def securities(self):
        return [{"ticker": tk, "bid": self.mid(tk) - 0.01, "ask": self.mid(tk) + 0.01, "last": self.mid(tk),
                 "position": self.pos[tk]} for tk in TICKERS]

    def leases(self):
        return [self.view(l) for l in self.store.values()]

    def assets(self):
        return [{"ticker": p, "lease_price": self.lease_price(p)} for p in ("AK-CS-PIPE", "CS-NYC-PIPE")]

    def news(self, after):
        return [n for n in self.items if n["news_id"] > after and kk(n["period"], n["tick"]) <= self.k]

    def nlv(self):
        v = self.cash + sum(self.pos[t] * SIZE[t] * self.mid(t) for t in TICKERS)
        for l in self.store.values():
            if l["ticker"] == "AK-CS-PIPE":
                v += l["_qty"] * 1000 * self.mid("CL")
            elif l["ticker"] == "CS-NYC-PIPE":
                v += l["_qty"] * 1000 * self.mid("CL-NYC")
            elif l["ticker"] == REFINERY and l.get("_finish") and l["_finish"] > self.k:
                v += 42000 * (10 * self.mid("HO") + 20 * self.mid("RB"))
        return v

    def trader(self):
        return {"nlv": self.nlv(), "total_fines": self.fines}

    def limits(self):
        return []

    def order(self, tk, action, qty):
        qty = int(qty)
        if qty <= 0 or qty > MAX_ORDER:
            return 0, None, "size"
        if tk == "CL-1F" and self.period != 1:
            return 0, None, "not tradeable"
        q = qty if action == "BUY" else -qty
        if q < 0 and tk not in FUTURES and self.pos[tk] + q < 0:
            return 0, None, "not shortable"
        if q > 0 and tk in TANK:
            cap = TANK_LOT * sum(1 for l in self.store.values() if l["ticker"] == TANK[tk])
            if self.pos[tk] + q > cap:
                return 0, None, "exceed storage capacity"
        net, gross = self.usage("CRUDE" if tk in CRUDE else "PRODUCT")
        g2 = gross - abs(self.pos[tk]) + abs(self.pos[tk] + q)
        if abs(net + q) > NET_LIMIT or (g2 > GROSS_LIMIT and g2 > gross):
            return 0, None, "limit"
        px = self.mid(tk) + (0.01 if q > 0 else -0.01)
        self.cash -= q * SIZE[tk] * px + qty * FEE
        self.cost["fees"] += qty * FEE
        self.pos[tk] += q
        return qty, px, ""

    def lease(self, ticker, **params):
        n = sum(1 for l in self.store.values() if l["ticker"] == ticker)
        if ticker in TANK.values():
            if n >= MAX_LEASES:
                return None, "lease limit"
            return self.view(self.add_lease(ticker)), ""
        if ticker in ("AK-CS-PIPE", "CS-NYC-PIPE"):
            src = params.get("from1")
            qty = int(params.get("quantity1") or 0)
            want = "CL-AK" if ticker == "AK-CS-PIPE" else "CL"
            if n >= MAX_LEASES or src != want or qty <= 0 or qty > PIPE_LOT or self.pos[src] < qty:
                return None, "pipe"
            price = self.lease_price(ticker)
            self.cash -= price
            self.cost["pipes"] += price
            self.pos[src] -= qty
            l = self.add_lease(ticker, convert_from=[{"ticker": src, "quantity": qty}])
            l["_qty"], l["_finish"] = qty, self.k + PIPE_TICKS
            self.stamp(l)
            return self.view(l), ""
        if ticker == REFINERY:
            if n >= 1:
                return None, "refinery busy"
            l = self.add_lease(ticker)
            l["_next"] = self.k + 60
            self.cash -= REFINERY_COST
            self.cost["refinery"] += REFINERY_COST
            self.stamp(l)
            return self.view(l), ""
        return None, "unknown"

    def use(self, lease_id, **params):
        l = self.store.get(lease_id)
        qty = int(params.get("quantity1") or 0)
        if not l or l["ticker"] != REFINERY or l.get("_finish") or qty != REFINERY_IN or self.pos["CL"] < qty:
            return None, "use"
        self.pos["CL"] -= qty
        l["_finish"] = self.k + REFINERY_TICKS
        l["convert_from"] = [{"ticker": "CL", "quantity": qty}]
        self.stamp(l)
        return self.view(l), ""

    def release(self, lease_id):
        l = self.store.get(lease_id)
        if not l:
            return False, "no lease"
        t = l["ticker"]
        if t in TANK.values():
            tk = next(s for s, st in TANK.items() if st == t)
            if self.pos[tk] > TANK_LOT * (sum(1 for x in self.store.values() if x["ticker"] == t) - 1):
                return False, "in use"
        if t in ("AK-CS-PIPE", "CS-NYC-PIPE"):
            return False, "pipe"
        del self.store[lease_id]
        return True, ""


def load_casefile(path):
    """Price paths ($TICKER sheets), headlines (news sheets) and the pipeline lease schedule (Variables)."""
    from openpyxl import load_workbook

    wb = load_workbook(path, read_only=True, data_only=True)
    paths = {}
    for tk in TICKERS:
        paths[tk] = [float(r[0]) for r in wb[f"${tk}"].iter_rows(values_only=True)
                     if r and isinstance(r[0], (int, float))]
    news = []
    for name in wb.sheetnames:
        if "news" not in name.lower():
            continue
        for row in wb[name].iter_rows(values_only=True):
            cells = list(row) + [None] * 5
            try:
                p, t = int(cells[0]), int(cells[1])
            except (TypeError, ValueError):
                continue
            news.append((p, t, str(cells[3] or "").strip(), str(cells[4] or "").strip()))
    news.sort(key=lambda x: (x[0], x[1]))
    n = 2 * TICKS
    lease = {"AK-CS-PIPE": [40_000.0] * n, "CS-NYC-PIPE": [20_000.0] * n}
    if "Variables" in wb.sheetnames:
        events = []
        for row in wb["Variables"].iter_rows(values_only=True):
            cells = list(row)
            if len(cells) >= 6 and isinstance(cells[0], (int, float)):
                p, t, kind, sub, var, val = cells[:6]
                if kind == "ASSETS" and sub in lease and var == "LeasePrice":
                    events.append((kk(p, t) - 1, sub, float(val)))
        for i, sub, val in sorted(events):
            if 0 <= i < n:
                lease[sub][i:] = [val] * (n - i)
    wb.close()
    return {"name": os.path.basename(path), "path": paths, "news": news, "lease": lease}


def backtest(case, cfg=None, start=1, log=None):
    """Run the Trader through one casefile. `start`: cumulative tick at which it joins."""
    ex = SimExchange(case)
    tr = Trader(ex, log or NullLog(), cfg)
    for k in range(1, ex.n + 1):
        ex.advance(k)
        if k >= start:
            tr.step(ex.period, ex.tick)
    return ex.nlv(), ex.cost, tr.stats


# ------------------------------------------------------------------------------ main
def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default=API_URL)
    p.add_argument("--user", default=API_USER)
    p.add_argument("--password", default=API_PASSWORD)
    p.add_argument("--check", action="store_true", help="pre-flight check of the connection and case; no trading")
    p.add_argument("--cases", type=int, default=0, help="stop after this many heats (0: run until Ctrl+C)")
    p.add_argument("--fresh", action="store_true", help="join a heat only in its first 3 ticks")
    p.add_argument("--max-seconds", type=float, default=0.0, help="wind down after this many seconds of a heat")
    p.add_argument("--set", action="append", default=[], metavar="NAME=value", help="override one CFG value")
    p.add_argument("--logs", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "com5_logs"))
    p.add_argument("--backtest", nargs="+", metavar="CASEFILE", help="replay RIT casefile(s) (.xlsx) instead")
    p.add_argument("--start", default="1", help="backtest: join at this tick, cumulative or P:T (e.g. 2:421)")
    a = p.parse_args()
    cfg = {}
    for item in a.set:
        name, value = item.split("=", 1)
        if name not in CFG:
            p.error(f"unknown setting {name}; settings: {', '.join(CFG)}")
        cfg[name] = type(CFG[name])(float(value))

    if a.backtest:
        start = kk(*a.start.split(":")) if ":" in a.start else int(a.start)
        results = []
        for path in a.backtest:
            case = load_casefile(path)
            nlv, cost, st = backtest(case, cfg, start)
            results.append(nlv)
            print(f"{case['name'][:60]:60s} NLV {nlv:12,.0f}   fines {cost['fines']:,.0f}  rejects {st['rejects']}  "
                  f"prints {st['prints']}  ships AK {st['ships_AK']} NYC {st['ships_NYC']}  batches {st['batches']}")
        if len(results) > 1:
            print(f"{'mean':60s} NLV {sum(results) / len(results):12,.0f}")
        return

    ex = LiveExchange(a.url, a.user, a.password)
    if a.check:
        raise SystemExit(0 if preflight(ex) else 1)
    signal.signal(signal.SIGINT, _sigint)
    me = ex.trader()
    print(f"trader {me.get('trader_id')} ({me.get('first_name', '')} {me.get('last_name', '')}) at {ex.url}"
          if me else f"NO CONNECTION to {ex.url} (retrying)", flush=True)
    done = 0
    while not stop_requested:
        try:
            if wait_for_case(ex, a.fresh) is None:
                break
        except KeyboardInterrupt:
            break
        run_heat(ex, cfg, a.logs, a.max_seconds)
        done += 1
        if stop_requested or (a.cases and done >= a.cases) or a.max_seconds:
            break
        time.sleep(2.0)


if __name__ == "__main__":
    main()
