"""RSM434 ALGO2e v3: forecast-driven market maker.

Run from the notebook (RSM434_Algo2e.ipynb) or directly:  python algo2e.py

What changed from v2, and why (the notebook's first cell has the evidence):
  * Quotes centre on a short-horizon forecast of the mid, not on the current microprice. The stocks
    behave differently: RY's mid snaps back within seconds after every swing, while CNR and AC keep
    moving for a second or two. v2 re-centred on every move, so it bought RY's spikes back at the top.
  * RY gets most of the position budget and a four-deep ladder that is filled by the sweeps that revert.
    In crowded rooms CNR is marginal and AC loses for market makers, so CNR is small and AC is off.
  * The forecast is checked against what actually happens during the run. If it stops predicting, its
    weight falls to zero and the algo degrades to a plain inventory-skewed market maker.
  * Limit fines cannot happen by construction: open orders are read before positions, every resting
    share counts against the cap, and a replacement order waits for the next position read.
  * RIT allows 100 requests a second per trader. A client-side token bucket keeps us under it, so the
    loop never stalls on HTTP 429, and a ticker trades on whatever reads succeeded this cycle.
  * Every live run is recorded to runs/ so the forecast can be refitted on our own data.
"""
import base64
import gzip
import json
import math
import os
import queue
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import requests
from requests.adapters import HTTPAdapter

BASE_URL = 'http://flserver.rotman.utoronto.ca:16585/v1'
USERNAME = '1010264928'
PASSWORD = 'fa'

# ---- case constants (case brief); fee and rebate are replaced by the server's values once the case is live
TICKERS = ('CNR', 'RY', 'AC')
TRADING_FEE = {'CNR': 0.0027, 'RY': -0.0014, 'AC': 0.0015}   # $/share on every fill
LIMIT_REBATE = {'CNR': 0.0050, 'RY': -0.0034, 'AC': 0.0026}  # $/share paid on top for passive fills
TICK, MAXQ, GROSS_LIMIT = 0.01, 5000, 25000

# ---- strategy, per ticker. Prices and edges are in ticks.
#   cap     largest |position|, 0 = do not trade; with WIDE on it is the crowded-room cap (RY's: its most)
#   ref     the cap the rung sizes below are written for; sizes scale with cap / ref
#   skew    reservation shift (ticks) at a full cap, pulling inventory back
#   rungs   (min edge, size) per resting order on each side; the first rung is the front of the book
#   take    edge beyond the taker fee needed to cross the spread
#   w, h    forecast weights on the features in Signal.features(), and the horizon (s) they forecast;
#           fitted out of sample on recorded practice cases by research/fit.py
#   mu_max  largest forecast we act on
CFG = {
    'CNR': dict(cap=4000, ref=4000, skew=2.0, rungs=((1.0, 2500), (3.0, 2000)), take=1.0, mu_max=4.0,
                h=3.0, w={'rev1': -1.59, 'imb': 1.59}),
    'RY':  dict(cap=20000, ref=20000, skew=1.0, rungs=((0.8, 5000), (2.0, 5000), (3.5, 5000), (5.5, 5000)),
                take=0.6, mu_max=6.0, h=3.0, w={'rev4': 2.40, 'rev8': -1.30}),
    'AC':  dict(cap=0, ref=7000, skew=2.0, rungs=((1.0, 4000), (2.0, 3000)), take=1.0, mu_max=4.0,
                h=1.0, w={'rev1': -0.53}),
}
#   WIDE    optional {ticker: (spread lo, spread hi, cap at hi)}: as that market's spread (EMA, ticks)
#           widens from lo to hi, its cap rises linearly to `cap at hi` and RY gets what is left of
#           BUDGET. Off by default: in every recorded case RY earned more per share of budget.
WIDE = {}
BUDGET     = 24000  # caps never sum past this; GROSS_LIMIT minus room for a fill that races a cancel
SPREAD_TAU = 20.0   # seconds of memory in the spread EMA that drives WIDE
SHED       = 0      # shares past a cap before we cross the spread to shed (raise it if WIDE lowers caps)
HORIZON   = 3.0    # default forecast horizon (s) when a ticker does not set h
CALIB     = 60.0   # seconds of memory behind the in-run forecast gain
PRIOR     = 30.0   # seconds of evidence the fitted weights are worth before the run says otherwise
GAIN_MAX  = 1.5
TOPUP     = 1000   # resize a resting order only when this far off target
BAND      = 1      # ticks a resting quote may sit behind its target before we move it (keeps queue priority)
HYST      = 0.5    # ticks of edge a resting quote may give up before we pull it
WIND      = 8      # final ticks: quote only to reduce inventory
CLOSE     = 2      # final ticks: flatten at market
STRICT    = True   # a cancelled order's room is released only after the next position read: a fill
                   # that lands between our read and the cancel can then never push us over a limit

RATE       = 92    # requests per second we allow ourselves (RIT: 100 per trader)
BURST      = 12
BOOK_DEPTH = 25
TIMEOUT    = 0.5
CASE_EVERY = 1.0   # seconds between /case reads; the tick in between is extrapolated from the clock
RECORD     = True  # write every cycle to runs/<time>.jsonl.gz for later refitting


def make_gain(t):
    """Net ticks earned per passive share (CNR +0.23, RY -0.20, AC +0.11)."""
    return (LIMIT_REBATE[t] - TRADING_FEE[t]) / TICK


def take_cost(t):
    """Ticks paid per share that removes liquidity (CNR +0.27, RY -0.14, AC +0.15)."""
    return TRADING_FEE[t] / TICK


# =====================================================================================================
# Forecast
# =====================================================================================================
class Signal:
    """Forecast of the mid change over the next `horizon` seconds, in ticks, from EMAs of the mid.

    Features (all in ticks):
      rev1..rev8  EMA(n s) - mid: positive weight = mean reversion, negative = momentum
      mom         EMA(1 s) - EMA(8 s): positive weight = momentum
      imb         microprice - mid: queue imbalance at the touch
      anon        ANON's own mid - mid: where the programmed liquidity is centred
    The weights come from fitting recorded cases (research/fit.py). During the run every forecast is
    scored against the mid `horizon` seconds later. The gain is the realized slope (realized ~ gain *
    forecast) over the last CALIB seconds, shrunk toward 1 by PRIOR seconds of make-believe evidence
    and clipped to [0, GAIN_MAX]: a forecast that stops working fades out instead of trading on."""
    TAUS = (1.0, 2.0, 4.0, 8.0)

    def __init__(self, w, mu_max, horizon=None):
        self.w, self.mu_max = w, mu_max
        self.h = horizon or HORIZON
        self.t = None
        self.e = {}
        self.pending = deque()           # (time, mid, raw forecast) waiting to be scored
        self.sxy = self.sxx = self.n = 0.0
        self.scored = None
        self.gain = 1.0
        self.raw = self.mu = 0.0

    def features(self, mid, micro, anon):
        return {'rev1': self.e[1.0] - mid, 'rev2': self.e[2.0] - mid, 'rev4': self.e[4.0] - mid,
                'rev8': self.e[8.0] - mid, 'mom': self.e[1.0] - self.e[8.0], 'imb': micro - mid,
                'anon': anon - mid}

    def update(self, now, mid, micro, anon=None):
        if self.t is None:
            self.e = {tau: mid for tau in self.TAUS}
        else:
            dt = max(0.0, now - self.t)
            for tau in self.TAUS:
                self.e[tau] += (1 - math.exp(-dt / tau)) * (mid - self.e[tau])
        self.t = now
        while self.pending and now - self.pending[0][0] >= self.h:
            t0, m0, f0 = self.pending.popleft()
            dt = 0.0 if self.scored is None else max(0.0, t0 - self.scored)
            self.scored = t0
            keep = math.exp(-dt / CALIB)
            self.sxy = self.sxy * keep + f0 * (mid - m0) * dt
            self.sxx = self.sxx * keep + f0 * f0 * dt
            self.n = self.n * keep + dt
        if self.sxx > 1e-9:
            g = (self.n * self.sxy / self.sxx + PRIOR) / (self.n + PRIOR)
            self.gain = min(GAIN_MAX, max(0.0, g))
        f = self.features(mid, micro, mid if anon is None else anon)
        raw = sum(self.w.get(k, 0.0) * v for k, v in f.items())
        self.pending.append((now, mid, raw))
        self.raw = raw
        self.mu = max(-self.mu_max, min(self.mu_max, self.gain * raw))
        return self.mu


# =====================================================================================================
# Strategy core: pure functions of the book, our orders and our position (shared with the backtest)
# =====================================================================================================
def touch(book):
    bids, asks = book['bids'], book['asks']
    bb, ba = bids[0][0], asks[0][0]
    bq = sum(q for p, q, *_ in bids if p == bb)
    aq = sum(q for p, q, *_ in asks if p == ba)
    return bb, ba, bq, aq


def anon_mid(book, mid):
    """Mid of ANON's best bid and ask, or `mid` when either is not in the depth we read."""
    b = next((o[0] for o in book['bids'] if o[2] == 'ANON'), None)
    a = next((o[0] for o in book['asks'] if o[2] == 'ANON'), None)
    return mid if b is None or a is None else (a + b) / 2


def observe(sig, book, now):
    """Feed one book into a Signal; returns the forecast in ticks."""
    bb, ba, bq, aq = touch(book)
    mid = (bb + ba) / 2
    return sig.update(now, mid, (bb * aq + ba * bq) / (bq + aq), anon_mid(book, mid))


def targets(t, c, book, pos, mu, tick, T):
    """Desired state for one ticker.

    book: other traders only, {'bids': [(price, qty, ...)], 'asks': [...]}, best first, prices in ticks.
    Returns (takes, rungs). takes: [(action, limit price, qty)] that cross now.
    rungs: [(action, price, lo, hi, qty)] resting quotes; a resting order anywhere in [lo, hi] is kept."""
    bb, ba, bq, aq = touch(book)
    cap, skew = c['cap'], c['skew']
    mid = (bb + ba) / 2
    res = mid + mu - skew * pos / cap
    mk, tk = make_gain(t), take_cost(t)
    wind = tick >= T - WIND

    takes, used = [], {'BUY': 0, 'SELL': 0}
    if abs(pos) > cap + SHED:                        # overfilled: shed the excess now
        a = 'SELL' if pos > 0 else 'BUY'
        q = min(abs(pos) - cap, MAXQ)
        takes.append((a, None, q))
        used[a] += q
    elif abs(pos) <= cap:
        for a, p, e, size, inv in (('BUY', ba, res - ba - tk, aq, -pos), ('SELL', bb, bb - res - tk, bq, pos)):
            if e <= c['take'] or (wind and inv <= 0):
                continue
            room = cap + inv                         # shares this side may add before reaching the cap
            q = int(min(size, MAXQ, room, math.ceil((e - c['take']) * cap / skew)))
            if wind:
                q = min(q, inv)
            if q > 0:
                takes.append((a, p, q))
                used[a] += q

    if wind:
        room = {'BUY': max(0, -pos), 'SELL': max(0, pos)}
    else:
        room = {'BUY': cap - pos, 'SELL': cap + pos}
    rungs = []
    for a in ('BUY', 'SELL'):
        left = room[a] - used[a]
        seen = {}
        for e_min, size in c['rungs']:
            if a == 'BUY':
                lim = math.floor(res + mk - e_min)
                p = min(bb + 1, ba - 1, lim)
                lo, hi = p - BAND, min(ba - 1, math.floor(res + mk - e_min + HYST))
            else:
                lim = math.ceil(res - mk + e_min)
                p = max(ba - 1, bb + 1, lim)
                lo, hi = p + BAND, max(bb + 1, math.ceil(res - mk + e_min - HYST))
            q = min(size, MAXQ, max(0, left))
            if q <= 0:
                break
            if p in seen:                            # two rungs landed on one price: merge
                i = seen[p]
                a0, p0, lo0, hi0, q0 = rungs[i]
                add = min(q, MAXQ - q0)
                rungs[i] = (a0, p0, lo0, hi0, q0 + add)
                left -= add
                continue
            seen[p] = len(rungs)
            rungs.append((a, p, lo, hi, q))
            left -= q
    # a 2-tick spread lets both fronts penny onto the same price: the side the forecast disfavours joins
    ib = next((i for i, r in enumerate(rungs) if r[0] == 'BUY'), None)
    ia = next((i for i, r in enumerate(rungs) if r[0] == 'SELL'), None)
    if ib is not None and ia is not None and rungs[ib][1] >= rungs[ia][1]:
        i, d = (ia, 1) if res > mid else (ib, -1)
        a, p, lo, hi, q = rungs[i]
        rungs[i] = (a, p + d, lo + d, hi + d, q)
    return takes, rungs


class Caps:
    """Splits BUDGET between the tickers from how wide each market's spread has been (see WIDE)."""

    def __init__(self):
        self.spread, self.t = {}, {}

    def observe(self, t, book, now):
        bb, ba, _, _ = touch(book)
        s = ba - bb
        if t not in self.spread:
            self.spread[t] = s
        else:
            self.spread[t] += (1 - math.exp(-max(0.0, now - self.t[t]) / SPREAD_TAU)) * (s - self.spread[t])
        self.t[t] = now

    def current(self, worst):
        """worst: per ticker, the exposure if every resting order on its heavier side fills.
        A ticker may only grow into budget the others leave free, so a cap that was just lowered
        (and is still being traded down to) is never handed to another ticker twice."""
        want = {}
        for t in TICKERS:
            cap = CFG[t]['cap']
            if t in WIDE and t in self.spread:
                lo, hi, top = WIDE[t]
                x = min(1.0, max(0.0, (self.spread[t] - lo) / (hi - lo)))
                cap = int(cap + x * (top - cap))
            want[t] = cap
        want['RY'] = max(0, min(CFG['RY']['cap'], BUDGET - sum(want[t] for t in TICKERS if t != 'RY')))
        held = {t: max(want[t], worst.get(t, 0)) for t in TICKERS}
        return {t: max(0, min(want[t], BUDGET - sum(held[u] for u in TICKERS if u != t))) for t in TICKERS}


def worst_case(pos, mine):
    """Exposure if every resting order on the heavier side fills. mine: [(id, action, price, remaining)]."""
    bids = sum(o[3] for o in mine if o[1] == 'BUY')
    asks = sum(o[3] for o in mine if o[1] == 'SELL')
    return max(pos + bids, asks - pos, abs(pos))


def sized(c, cap):
    """The ticker's config at another cap: rung sizes scale with cap / ref, never past MAXQ."""
    if cap == c['cap']:
        return c
    k = cap / c.get('ref', c['cap'] or 1)
    return dict(c, cap=cap, rungs=tuple((e, int(min(MAXQ, s * k))) for e, s in c['rungs']))


def reconcile(rungs, mine):
    """Match wanted rungs to resting orders, touching as few as possible.

    mine: [(order_id, action, price, remaining)]. Returns (cancel ids, new [(action, price, qty)])."""
    keep, cancel, post = set(), [], []
    for a, p, lo, hi, q in rungs:
        lo, hi = min(lo, hi), max(lo, hi)
        group = [o for o in mine if o[1] == a and lo <= o[2] <= hi and o[0] not in keep]
        group.sort(key=lambda o: abs(o[2] - p))
        have = 0
        for o in group:
            if have >= q:
                break
            keep.add(o[0])
            have += o[3]
        if have > q + TOPUP:                         # oversized: replace the whole rung
            for o in group:
                keep.discard(o[0])
            have = 0
        if q > have + (TOPUP if have else 0):        # missing or short: add at the target price
            post.append((a, p, q - have))
    cancel = [o[0] for o in mine if o[0] not in keep]
    return cancel, post


def plan(t, c, book, mine, pos, mu, tick, T):
    """One ticker -> (cancel ids, orders to send now, orders to send once those cancels are confirmed).

    Orders are (action, price or None for market, qty). Every resting share counts against the cap,
    including shares whose cancel is in flight: an order only goes out now if it fits the room left
    when all of those fill. The rest wait for the cancel confirmation (or the next cycle if STRICT)."""
    if tick >= T - CLOSE or c['cap'] <= 0:           # end of the case, or a ticker switched off
        flat = [('SELL' if pos > 0 else 'BUY', None, min(abs(pos), MAXQ))] if pos else []
        return [o[0] for o in mine], [], flat
    takes, rungs = targets(t, c, book, pos, mu, tick, T)
    cancel, post = reconcile(rungs, mine)
    cap = c['cap']
    live = {'BUY': sum(o[3] for o in mine if o[1] == 'BUY'), 'SELL': sum(o[3] for o in mine if o[1] == 'SELL')}
    gone = {'BUY': sum(o[3] for o in mine if o[1] == 'BUY' and o[0] in cancel),
            'SELL': sum(o[3] for o in mine if o[1] == 'SELL' and o[0] in cancel)}
    # free_now: room if every resting share fills, including those being cancelled (what orders sent
    # now may use). free_later: room once the cancels are confirmed. Everything sent, now or later,
    # must fit free_later, and what is sent now must also fit free_now.
    free_now = {'BUY': cap - pos - live['BUY'], 'SELL': cap + pos - live['SELL']}
    free_later = {a: free_now[a] + (0 if STRICT else gone[a]) for a in free_now}
    hi_bid = max((o[2] for o in mine if o[1] == 'BUY' and o[0] not in cancel), default=-math.inf)
    lo_ask = min((o[2] for o in mine if o[1] == 'SELL' and o[0] not in cancel), default=math.inf)
    hi_bid_any = max((o[2] for o in mine if o[1] == 'BUY'), default=-math.inf)
    lo_ask_any = min((o[2] for o in mine if o[1] == 'SELL'), default=math.inf)

    now, later = [], []
    for a, p, q in list(takes) + post:
        reduce = (a == 'SELL' and pos > 0) or (a == 'BUY' and pos < 0)
        # would trade with our own resting order: only after the cancels on that side are confirmed
        if a == 'BUY':
            cross_kept = lo_ask < math.inf if p is None else p >= lo_ask
            cross_any = lo_ask_any < math.inf if p is None else p >= lo_ask_any
        else:
            cross_kept = hi_bid > -math.inf if p is None else p <= hi_bid
            cross_any = hi_bid_any > -math.inf if p is None else p <= hi_bid_any
        if cross_kept:
            continue
        if p is None and reduce:                     # shedding at market never adds exposure
            (later if cross_any else now).append((a, p, q))
            continue
        if not cross_any and q <= free_now[a]:
            now.append((a, p, q))
            free_now[a] -= q
            free_later[a] -= q
            continue
        q = min(q, free_later[a])
        if q >= 100:
            later.append((a, p, q))
            free_later[a] -= q
            free_now[a] = min(free_now[a], free_later[a])
    return cancel, now, later


# =====================================================================================================
# REST
# =====================================================================================================
class Bucket:
    """Client-side token bucket so the loop stays under RIT's per-trader request limit."""

    def __init__(self, rate, burst):
        self.rate, self.burst, self.tokens = rate, burst, float(burst)
        self.t = time.perf_counter()
        self.lock = threading.Lock()

    def take(self):
        while True:
            with self.lock:
                now = time.perf_counter()
                self.tokens = min(self.burst, self.tokens + (now - self.t) * self.rate)
                self.t = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                wait = (1 - self.tokens) / self.rate
            time.sleep(wait)


s = requests.Session()
s.headers['Authorization'] = 'Basic ' + base64.b64encode(f'{USERNAME}:{PASSWORD}'.encode()).decode()
s.mount('http://', HTTPAdapter(pool_maxsize=32))
POOL = ThreadPoolExecutor(32)
BUCKET = Bucket(RATE, BURST)
NET = defaultdict(lambda: {'ms': [], '429': 0, 'err': 0})
STATS = defaultdict(int)


def api(method, path, timeout=TIMEOUT, retry=1, **params):
    """One request -> parsed JSON, or None. An HTTP 429 is retried after the wait RIT asks for."""
    net = NET[f'{method} {path}']
    for attempt in range(retry + 1):
        BUCKET.take()
        t0 = time.perf_counter()
        try:
            r = s.request(method, f'{BASE_URL}/{path}', params=params, timeout=timeout)
        except requests.RequestException:
            net['err'] += 1
            return None
        finally:
            net['ms'].append((time.perf_counter() - t0) * 1000)
        if r.status_code == 429:
            net['429'] += 1
            STATS['http429'] += 1
            try:
                wait = float(r.headers.get('Retry-After') or r.json().get('wait', 0.01))
            except ValueError:
                wait = 0.01
            time.sleep(min(wait, 0.25))
            continue
        if r.ok:
            try:
                return r.json()
            except ValueError:
                return None
        net['err'] += 1
        return None
    return None


def send(t, action, p, q):
    if p is None:
        return api('POST', 'orders', ticker=t, type='MARKET', quantity=int(q), action=action)
    return api('POST', 'orders', ticker=t, type='LIMIT', quantity=int(q), action=action, price=round(p * TICK, 2))


def execute(cancel, now, later, need):
    """Cancels and safe orders go out together. The rest wait until every cancel on their ticker is
    confirmed; a cancel that is not confirmed means that order filled, so its ticker waits a cycle."""
    jobs = [POOL.submit(send, *o) for o in now]
    done = set()
    if cancel:
        out = api('POST', 'commands/cancel', retry=2, ids=','.join(map(str, cancel)))
        done = set((out or {}).get('cancelled_order_ids') or [])
    ready = [o for o in later if need[o[0]] <= done]
    jobs += [POOL.submit(send, *o) for o in ready]
    STATS['sent'] += len(now) + len(ready)
    STATS['cancels'] += len(cancel)
    STATS['held'] += len(later) - len(ready)
    for j in jobs:
        j.result()


# =====================================================================================================
# Live loop
# =====================================================================================================
ME = None
SIG = {}
CAPS = None
TRACE = []      # per tick: (tick, {t: (pos, realized, unrealized, gain, mu)})
WORK = []


class Recorder:
    """Writes cycles to runs/<time>.jsonl.gz from a background thread, so the loop never waits on disk."""

    def __init__(self):
        os.makedirs('runs', exist_ok=True)
        self.path = os.path.join('runs', datetime.now().strftime('%Y%m%d-%H%M%S') + '.jsonl.gz')
        self.q = queue.Queue()
        self.th = threading.Thread(target=self.loop, daemon=True)
        self.th.start()

    def put(self, row):
        self.q.put(row)

    def loop(self):
        with gzip.open(self.path, 'wt') as f:
            while True:
                row = self.q.get()
                if row is None:
                    return
                f.write(json.dumps(row, separators=(',', ':')) + '\n')

    def close(self):
        self.q.put(None)
        self.th.join(timeout=5)


def load_fees(case, sec):
    lines = [f"{case.get('name', '?')} is live. Per share, server (case brief):"]
    for d in sec:
        t = d['ticker']
        if t not in TICKERS or d.get('trading_fee') is None or d.get('limit_order_rebate') is None:
            continue
        old = (TRADING_FEE[t], LIMIT_REBATE[t])
        TRADING_FEE[t], LIMIT_REBATE[t] = d['trading_fee'], d['limit_order_rebate']
        lines.append(f"  {t:4} fee {TRADING_FEE[t]:+.4f} ({old[0]:+.4f})  rebate {LIMIT_REBATE[t]:+.4f} "
                     f"({old[1]:+.4f})  -> passive {make_gain(t):+.2f} ticks, active {-take_cost(t):+.2f} ticks")
    return '\n'.join(lines)


def split_book(raw):
    """RIT book JSON -> other traders' resting orders, (price in ticks, remaining, trader, order id)."""
    book = {'bids': [], 'asks': []}
    for side in ('bids', 'asks'):
        for o in raw.get(side) or []:
            if o['trader_id'] == ME:
                continue
            q = o['quantity'] - o['quantity_filled']
            if q > 0:
                book[side].append((round(o['price'] / TICK), q, o['trader_id'], o['order_id']))
    return book


def main():
    global ME, CAPS
    STATS.clear()
    NET.clear()
    TRACE.clear()
    WORK.clear()
    me = api('GET', 'trader', timeout=5)
    if not me:
        print('Cannot reach RIT: check BASE_URL, USERNAME and PASSWORD.')
        return
    ME = me['trader_id']
    SIG.clear()
    SIG.update({t: Signal(CFG[t]['w'], CFG[t]['mu_max'], CFG[t].get('h')) for t in TICKERS})
    CAPS = Caps()
    assert BUDGET <= GROSS_LIMIT, f'BUDGET {BUDGET} > {GROSS_LIMIT}'
    print(f'{ME} connected, waiting for the case.', flush=True)
    rec = None
    live, case, case_at, last_tick = False, None, -math.inf, -1
    try:
        while True:
            t0 = time.perf_counter()
            if t0 - case_at >= CASE_EVERY or not live:
                c = api('GET', 'case')
                if c:
                    case, case_at = c, t0
            if not case or case['status'] != 'ACTIVE':
                if live and case and case['status'] == 'STOPPED':
                    break
                time.sleep(0.2)
                continue
            tick = min(case['ticks_per_period'], case['tick'] + int(t0 - case_at))
            T = case['ticks_per_period']

            # reads: books and our orders together; positions only after the orders, so that every fill
            # is either in the position or still in an order's remaining size (never in neither)
            fb = {t: POOL.submit(api, 'GET', 'securities/book', retry=0, ticker=t, limit=BOOK_DEPTH) for t in TICKERS}
            orders = api('GET', 'orders', retry=0)
            sec = api('GET', 'securities', retry=0) if orders is not None else None
            raw = {t: f.result() for t, f in fb.items()}
            if orders is None or sec is None:
                STATS['miss'] += 1
                continue
            if not live:
                live = True
                print(load_fees(case, sec), flush=True)
                if RECORD:
                    rec = Recorder()
            now = time.time()
            pos = {d['ticker']: int(d['position']) for d in sec if d['ticker'] in TICKERS}
            mine = {t: [(o['order_id'], o['action'], round(o['price'] / TICK), o['quantity'] - o['quantity_filled'])
                        for o in orders if o['ticker'] == t and o['type'] == 'LIMIT'] for t in TICKERS}
            books, mus = {}, {}
            for t in TICKERS:
                if not raw[t]:
                    STATS['book_miss'] += 1
                    continue
                book = split_book(raw[t])
                if book['bids'] and book['asks']:
                    books[t] = book
                    mus[t] = observe(SIG[t], book, now)
                    CAPS.observe(t, book, now)
            caps = CAPS.current({t: worst_case(pos.get(t, 0), mine[t]) for t in TICKERS})
            cancel, send_now, send_later, need = [], [], [], {}
            row = {'wall': now, 'tick': tick, 'pos': pos, 'caps': caps} if rec else None
            for t, book in books.items():
                c, n, l = plan(t, sized(CFG[t], caps[t]), book, mine[t], pos.get(t, 0), mus[t], tick, T)
                cancel += c
                need[t] = set(c)
                send_now += [(t, *o) for o in n]
                send_later += [(t, *o) for o in l]
                if row is not None:
                    row[t] = {'b': raw[t]['bids'][:BOOK_DEPTH], 'a': raw[t]['asks'][:BOOK_DEPTH],
                              'mu': round(mus[t], 3), 'gain': round(SIG[t].gain, 3)}
            execute(cancel, send_now, send_later, need)
            STATS['cycles'] += 1
            work = (time.perf_counter() - t0) * 1000
            WORK.append(work)
            if row is not None:
                row['orders'] = [(o['order_id'], o['ticker'], o['action'], o['price'], o['quantity'],
                                  o['quantity_filled']) for o in orders]
                row['sent'] = send_now + send_later
                row['cancel'] = cancel
                rec.put(row)
            if tick != last_tick:
                last_tick = tick
                by = {d['ticker']: d for d in sec}
                TRACE.append((tick, {t: (pos.get(t, 0), float(by[t].get('realized') or 0),
                                         float(by[t].get('unrealized') or 0), SIG[t].gain, SIG[t].mu)
                                     for t in TICKERS if t in by}))
                if tick % 10 == 0:
                    pnl = sum(v[1] + v[2] for v in TRACE[-1][1].values())
                    print(f'tick {tick:3d} | pnl {pnl:+10.2f} | '
                          + '  '.join(f'{t} {v[0]:+6d} g{v[3]:.1f} mu{v[4]:+.1f}' for t, v in TRACE[-1][1].items())
                          + f' | {work:.0f} ms', flush=True)
    finally:
        api('POST', 'commands/cancel', timeout=5, retry=3, all=1)
        print('Stopped, all orders cancelled.', flush=True)
        if rec:
            rec.close()
            print(f'Recorded this run to {rec.path}', flush=True)
        try:
            report()
        except Exception as exc:
            print('Report failed:', exc, flush=True)


# =====================================================================================================
# Report (read-only, after the case)
# =====================================================================================================
def _pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round((len(xs) - 1) * p)))] if xs else float('nan')


def report():
    case = api('GET', 'case', timeout=5, retry=3) or {}
    trader = api('GET', 'trader', timeout=5, retry=3) or {}
    sec = api('GET', 'securities', timeout=5, retry=3) or []
    tx = api('GET', 'transactions', timeout=5, retry=3, limit=100000) or []
    lines = []
    add = lines.append
    add(f"{case.get('name', '?')}  tick {case.get('tick', '?')}/{case.get('ticks_per_period', '?')}  {case.get('status', '?')}")
    add(f"trader {trader.get('trader_id', '?')}   NLV {trader.get('nlv', 0):,.2f}   fines {trader.get('total_fines', 0):,.2f}")
    add('')
    add(f"{'':5} {'pos':>7} {'realized':>11} {'unrealized':>11} {'volume':>11}")
    for d in sec:
        if d['ticker'] in TICKERS:
            add(f"{d['ticker']:5} {d.get('position', 0):7.0f} {d.get('realized') or 0:11.2f} "
                f"{d.get('unrealized') or 0:11.2f} {d.get('volume') or 0:11.0f}")
    cash, n = defaultdict(float), defaultdict(int)
    for r in tx:
        k = (r.get('ticker') or '-', r.get('type') or '?')
        cash[k] += float(r.get('cash_flow') or 0)
        n[k] += 1
    if cash:
        add('')
        add(f"  {'ticker':11} {'type':18} {'n':>6} {'cash':>12}")
        for k in sorted(cash):
            add(f"  {k[0]:11} {k[1]:18} {n[k]:6d} {cash[k]:12.2f}")
    add('')
    if WORK:
        add(f"cycles {STATS['cycles']}  missed {STATS['miss']}  book misses {STATS['book_miss']}  "
            f"HTTP 429 {STATS['http429']}  sent {STATS['sent']}  cancels {STATS['cancels']}  held {STATS['held']}")
        add(f"cycle ms  median {_pct(WORK, .5):.0f}  p95 {_pct(WORK, .95):.0f}  max {max(WORK):.0f}")
    for k, v in sorted(NET.items()):
        if v['ms']:
            add(f"  {k:24} n {len(v['ms']):6d}  p50 {_pct(v['ms'], .5):5.0f} ms  p95 {_pct(v['ms'], .95):5.0f} ms"
                f"  429 {v['429']:5d}  failed {v['err']:4d}")
    if TRACE:
        add('')
        add('forecast gain at the end: ' + '  '.join(f"{t} {SIG[t].gain:.2f}" for t in TICKERS if t in SIG))
        add('path (tick, P&L, positions)')
        for tick, row in TRACE:
            if tick % 30 == 0 or (tick, row) == TRACE[-1]:
                add(f"  {tick:3d} {sum(v[1] + v[2] for v in row.values()):+11.2f}   "
                    + '  '.join(f'{t} {v[0]:+6d}' for t, v in row.items()))
    text = '\n'.join(lines)
    print('\n' + text, flush=True)
    os.makedirs('diagnostics', exist_ok=True)
    path = os.path.join('diagnostics', datetime.now().strftime('%Y%m%d-%H%M%S') + '.txt')
    for name in (path, os.path.join('diagnostics', 'latest.txt')):
        with open(name, 'w', encoding='utf-8') as f:
            f.write(text + '\n')
    print(f'Saved {path}', flush=True)
    return text


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
