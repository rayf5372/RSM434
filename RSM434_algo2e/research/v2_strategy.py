"""v2 settings and strategy, copied verbatim from RSM434_Algo2e.ipynb (git af0349d) for the backtest baseline."""
import base64
import math
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

import requests
from requests.adapters import HTTPAdapter

BASE_URL = 'http://flserver.rotman.utoronto.ca:16585/v1'
USERNAME = '1010264928'
PASSWORD = 'fa'

# ---- case constants (case brief). FEE and REBATE are replaced by the server's values once the case is live.
TICKERS = ('CNR', 'RY', 'AC')
FEE    = {'CNR': 0.0027, 'RY': -0.0014, 'AC': 0.0015}   # per share, charged on every fill, passive or active
REBATE = {'CNR': 0.0023, 'RY': -0.0020, 'AC': 0.0011}   # net per passive share: the server's limit_order_rebate minus FEE
BRIEF  = {t: (FEE[t], REBATE[t]) for t in TICKERS}
TICK, MAXQ, LIMIT = 0.01, 5000, 25000

# ---- strategy (prices and edges in ticks)
CAP       = int(0.96 * LIMIT / len(TICKERS))  # per-ticker inventory cap; all caps together stay inside LIMIT
SKEW      = 2.0    # reservation-price shift at a full cap
TAKE_EDGE = 0.5    # edge left after the taker fee before we cross the spread to shed inventory
TOPUP     = 1000   # resize a resting order only when this far off target
WIND      = 8      # final ticks: quote only to reduce inventory
CLOSE     = 2      # final ticks: flatten at market
STICKY    = 1      # ticks a resting quote may sit behind its target before we chase (keeps queue priority)
DEEP      = 2      # second order sits this many ticks behind the front one; must exceed STICKY
PROBE     = 15     # every PROBE ticks, retry FAST in case rivals have left
ALPHA, TO_SLOW, TO_FAST = 0.02, 0.5, 0.3      # race-loss EMA and its hysteresis band
STRICT_ROOM = False  # True: orders still being cancelled count against CAP until confirmed (no overfill, but a reprice waits a round trip)
MODES = {  # edge: min edge per fill | penny: ticks inside the best price | front: front size, the rest goes deep
    'FAST': dict(edge=0.3, penny=1, front=MAXQ),
    'SLOW': dict(edge=0.4, penny=0, front=3000),
}
PACE       = 0.01  # fastest cycle in seconds; an HTTP 429 adds only the wait RIT asks for
BOOK_DEPTH = 20
TIMEOUT    = 0.5   # seconds per trading request; a slow one is dropped and retried next cycle
CASE_EVERY = 0.2   # seconds between /case reads; only the tick and status come from it

# ---- connection: one keep-alive session shared by a thread pool
s = requests.Session()
s.headers['Authorization'] = 'Basic ' + base64.b64encode(f'{USERNAME}:{PASSWORD}'.encode()).decode()
s.mount('http://', HTTPAdapter(pool_maxsize=32))
POOL = ThreadPoolExecutor(32)
WAIT = [0.0]       # longest Retry-After seen this cycle

# Diagnostics stay in memory during the case. report() reads the server only after it stops.
STATS  = {'http429': 0, 'cycles': 0, 'miss': 0, 'sent': 0, 'cancels': 0, 'held': 0}
NET    = defaultdict(lambda: {'ms': [], '429': 0, 'err': 0})   # per endpoint: latency, rate limits, failures
WORK   = []          # work time of every cycle, ms
TRACE  = []          # one row per tick, taken from the snapshot the loop already fetched
RACES  = {}          # per ticker: [races won, races lost] as scored by lost()
RIVALS = set()       # non-ANON trader ids seen in the books
SPAN   = [0.0, 0.0]  # perf_counter at the first and the last live cycle


P   = lambda o: round(o['price'] / TICK)             # price -> integer ticks
rem = lambda o: o['quantity'] - o['quantity_filled']
ME, LAG, MODE = None, {}, {}


def load_fees(case, sec):
    """Quote with the server's fee and net passive rebate, and report where they differ from the case brief.
    RIT charges the fee on every fill and pays limit_order_rebate on top for passive ones, so the net
    passive rebate is the difference of the two."""
    by = {d['ticker']: d for d in sec}
    lines = [f"{case.get('name', '?')} is live. Per share, server (case brief):"]
    for t in TICKERS:
        d = by.get(t, {})
        fee, reb = BRIEF[t]
        if d.get('trading_fee') is None or d.get('limit_order_rebate') is None:
            lines.append(f'  {t:4} not reported by the server, quoting with the case brief')
            continue
        FEE[t], REBATE[t] = d['trading_fee'], d['limit_order_rebate'] - d['trading_fee']
        differ = abs(FEE[t] - fee) > 1e-9 or abs(REBATE[t] - reb) > 1e-9
        lines.append(f"  {t:4} fee {FEE[t]:+.4f} ({fee:+.4f})   net passive rebate {REBATE[t]:+.4f} ({reb:+.4f})"
                     + ('   DIFFERENT from the brief, quoting with the server values' if differ else ''))
    return '\n'.join(lines)


def lost(side, lim, d):
    """1 if a rival holds the front at a price we would quote, 0 if we do, None if there was no race.
    lim is our most aggressive acceptable price; d is +1 for bids, -1 for asks."""
    mine = next((o for o in side if o['trader_id'] == ME), None)
    rival = next((o for o in side if o['trader_id'] not in (ME, 'ANON') and d * (lim - P(o)) >= 0), None)
    if mine is None or rival is None:
        return None
    return int(d * (P(rival) - P(mine)) > 0 or P(rival) == P(mine) and rival['order_id'] < mine['order_id'])


def reconcile(want, mine):
    """Diff target quotes (action, price, qty, band) against resting orders, touching as few as possible."""
    rest = {}
    for o in mine:
        g = rest.setdefault((o['action'], P(o)), [0, []])
        g[0] += rem(o)
        g[1].append(o['order_id'])
    cancel, post = [], []
    for a, p, q, band in want:
        d = 1 if a == 'BUY' else -1                  # a resting order may sit up to `band` ticks less aggressive
        key = next((k for k in ((a, p - d * i) for i in range(band + 1)) if k in rest), None)
        have, ids = rest.pop(key) if key else (0, [])
        if key and (not q or have > q + TOPUP):      # unwanted or oversized: replace
            cancel += ids
            key, have = None, 0
        if q > have + (TOPUP if key else 0):         # missing or under-filled: add (keeps the rest's priority)
            post.append((a, key[1] if key else p, q - have))
    for _, ids in rest.values():
        cancel += ids
    return cancel, post


def plan(t, book, mine, pos, tick, T):
    """One ticker -> (ids to cancel, orders to send now, orders to send once those cancels are confirmed)."""
    if tick >= T - CLOSE:                            # last seconds: flatten at market
        flat = [('SELL' if pos > 0 else 'BUY', None, min(abs(pos), MAXQ))] if pos else []
        return [o['order_id'] for o in mine], [], flat

    bids = [o for o in book['bids'] if o['trader_id'] != ME]
    asks = [o for o in book['asks'] if o['trader_id'] != ME]
    if not bids or not asks:
        return [], [], []
    bb, ba = P(bids[0]), P(asks[0])
    bq = sum(rem(o) for o in bids if P(o) == bb)
    aq = sum(rem(o) for o in asks if P(o) == ba)
    r, f = REBATE[t] / TICK, FEE[t] / TICK
    res = (bb * aq + ba * bq) / (bq + aq) - SKEW * pos / CAP      # microprice shifted against inventory

    edge = MODES[MODE[t]]['edge']                    # are we the fastest on this ticker?
    for side, lim, d in ((book['bids'], math.floor(res + r - edge), 1), (book['asks'], math.ceil(res - r + edge), -1)):
        x = lost(side, lim, d)
        if x is not None:
            LAG[t] += ALPHA * (x - LAG[t])
            RACES[t][x] += 1
    RIVALS.update(o['trader_id'] for o in book['bids'] + book['asks'] if o['trader_id'] not in (ME, 'ANON'))
    MODE[t] = 'SLOW' if LAG[t] > TO_SLOW else 'FAST' if LAG[t] < TO_FAST else MODE[t]
    # SLOW only stops re-pennying a rival; a side whose best price is ANON's is pennied regardless.
    mb = MODES['FAST' if bids[0]['trader_id'] == 'ANON' else MODE[t]]
    ma = MODES['FAST' if asks[0]['trader_id'] == 'ANON' else MODE[t]]

    take = []
    if abs(pos) > CAP:                               # overfilled: the limit fine is $1/share, so shed the excess at market
        take.append(('SELL' if pos > 0 else 'BUY', None, min(abs(pos) - CAP, MAXQ)))
    else:                                            # cross to shed inventory once the skew pays the taker fee
        for a, p, e, size, inv in (('SELL', bb, bb - res - f, bq, pos), ('BUY', ba, res - ba - f, aq, -pos)):
            if inv > 0 and e > TAKE_EDGE:
                take.append((a, p, int(min(inv, size, MAXQ, math.ceil((e - TAKE_EDGE) * CAP / SKEW)))))

    bid = min(bb + mb['penny'], ba - 1, math.floor(res + r - mb['edge']))   # most aggressive price that clears the edge
    ask = max(ba - ma['penny'], bb + 1, math.ceil(res - r + ma['edge']))
    if bid >= ask:                                   # 2-tick spread: only one side can penny
        bid, ask = (bid, bid + 1) if res > (bb + ba) / 2 else (ask - 1, ask)
    rb, ra = (max(0, -pos), max(0, pos)) if tick >= T - WIND else (CAP - pos, CAP + pos)
    want = []
    for a, p, room, d, m in (('BUY', bid, rb, -1, mb), ('SELL', ask, ra, 1, ma)):
        q0 = max(0, min(room, m['front']))
        want += [(a, p, q0, STICKY), (a, p + d * DEEP, max(0, min(room - q0, MAXQ)), STICKY)]
    cancel, post = reconcile(want, mine)

    hi_bid = max((P(o) for o in mine if o['action'] == 'BUY'), default=-math.inf)
    lo_ask = min((P(o) for o in mine if o['action'] == 'SELL'), default=math.inf)

    def crosses(o):                                  # would trade with ourselves; a market order crosses any order of ours
        if o[0] == 'BUY':
            return lo_ask < math.inf if o[1] is None else o[1] >= lo_ask
        return hi_bid > -math.inf if o[1] is None else o[1] <= hi_bid

    free = {'BUY': rb, 'SELL': ra}                   # room left if every resting order fills, cancelled ones included
    for o in mine:
        free[o['action']] -= rem(o)
    now, later = [], []
    for sheds, group in ((True, take), (False, post)):
        for o in group:
            safe = not crosses(o) and (sheds or not STRICT_ROOM or o[2] <= free[o[0]])
            (now if safe else later).append(o)
            free[o[0]] -= o[2]
    return cancel, now, later
