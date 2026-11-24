"""Replay recorded cases through a strategy, with queue-position fills.

    python research/backtest.py                 # v3 and v2 on every full recording
    python research/backtest.py --delay 1       # orders and cancels take effect one snapshot later

Fill model, per resting order of ours:
  * it joins the back of its price level: `ahead` = other traders' size there when it arrives;
    size that later leaves that level without trading moves us up (ahead = min(ahead, size left));
  * a print at our price first uses up `ahead`, the rest fills us;
  * a print at a worse price (below our bid, above our ask) means incoming flow went past our level,
    so it would have filled us first;
  * if the other side's best price reaches ours, we are filled as the passive side.
Our marketable orders fill against the visible book (and that liquidity stays used up for us).
What it cannot show: how rivals react to our quotes, and our own price impact.
"""
import argparse
import os
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..'))

import algo2e as A  # noqa: E402
from data import TICKERS, files, load  # noqa: E402

T_END = 300


class Book:
    """Other traders' orders at one snapshot, less whatever our marketable orders already took."""

    def __init__(self, raw, used):
        self.bids = [(p, q - used.get(oid, 0), who, oid) for p, q, who, oid in raw['bids'] if q - used.get(oid, 0) > 0]
        self.asks = [(p, q - used.get(oid, 0), who, oid) for p, q, who, oid in raw['asks'] if q - used.get(oid, 0) > 0]

    def size_at(self, side, p):
        return sum(q for pp, q, *_ in (self.bids if side == 'BUY' else self.asks) if pp == p)

    def as_dict(self):
        return {'bids': self.bids, 'asks': self.asks}


class Sim:
    def __init__(self, snaps, strategy, delay=0):
        self.snaps, self.strat, self.delay = snaps, strategy, delay
        self.pos = defaultdict(int)
        self.cash = defaultdict(float)
        self.fees = defaultdict(float)
        self.vol = defaultdict(lambda: defaultdict(float))
        self.orders = {}            # id -> dict(t, a, p, rem, ahead, live_from)
        self.used = {t: {} for t in TICKERS}
        self.next_id = 1
        self.fines = 0.0
        self.max_gross = 0
        self.pending = []           # (effective index, kind, payload)
        self.path = []
        self.held = 0
        self.over = 0               # snapshots spent above the gross limit

    # -- fills ----------------------------------------------------------------------------------------
    def fill(self, t, a, p, q, passive):
        q = int(q)
        if q <= 0:
            return
        sgn = 1 if a == 'BUY' else -1
        self.pos[t] += sgn * q
        self.cash[t] -= sgn * q * p * A.TICK
        fee = q * (A.LIMIT_REBATE[t] - A.TRADING_FEE[t]) if passive else -q * A.TRADING_FEE[t]
        self.cash[t] += fee
        self.fees[t] += fee
        self.vol[t]['passive' if passive else 'active'] += q

    def passive_fills(self, k, snap):
        for oid, o in list(self.orders.items()):
            if o['live_from'] > k:
                continue
            t, a, p = o['t'], o['a'], o['p']
            book = self.books[t]
            o['ahead'] = min(o['ahead'], book.size_at(a, p))
            for pt, qt in snap[t]['trades']:
                if o['rem'] <= 0:
                    break
                if (a == 'BUY' and pt < p) or (a == 'SELL' and pt > p):
                    x = min(o['rem'], qt)
                elif pt == p:
                    eat = min(o['ahead'], qt)
                    o['ahead'] -= eat
                    x = min(o['rem'], qt - eat)
                else:
                    continue
                self.fill(t, a, p, x, True)
                o['rem'] -= x
            if o['rem'] > 0:                                   # the other side moved through our price
                cross = book.asks if a == 'BUY' else book.bids
                avail = sum(q for pp, q, *_ in cross if (pp <= p if a == 'BUY' else pp >= p))
                x = min(o['rem'], avail)
                if x > 0:
                    self.fill(t, a, p, x, True)
                    o['rem'] -= x
            if o['rem'] <= 0:
                del self.orders[oid]

    def take(self, k, t, a, lim, q):
        """Marketable order: sweep the visible book up to `lim` (None = market); the rest rests at `lim`."""
        book = self.books[t]
        levels = book.asks if a == 'BUY' else book.bids
        left = int(q)
        for p, qq, who, oid in levels:
            if left <= 0 or (lim is not None and (p > lim if a == 'BUY' else p < lim)):
                break
            x = min(left, qq)
            self.fill(t, a, p, x, False)
            self.used[t][oid] = self.used[t].get(oid, 0) + x
            left -= x
        self.books[t] = Book(self.snaps[k][t], self.used[t])
        if left > 0 and lim is not None:
            self.rest(k, t, a, lim, left, live_from=k + 1)

    def rest(self, k, t, a, p, q, live_from):
        oid = self.next_id
        self.next_id += 1
        self.orders[oid] = dict(t=t, a=a, p=p, rem=int(q), ahead=self.books[t].size_at(a, p), live_from=live_from)

    # -- main loop ------------------------------------------------------------------------------------
    def run(self):
        last_tick = None
        self.books = {}
        for k, snap in enumerate(self.snaps):
            self.books = {t: Book(snap[t], self.used[t]) for t in TICKERS}
            self.passive_fills(k, snap)
            # actions decided `delay` snapshots ago reach the exchange after this interval's prints
            due = [x for x in self.pending if x[0] <= k]
            self.pending = [x for x in self.pending if x[0] > k]
            for _, kind, pay in due:
                self.apply(k, kind, pay)
            gross = sum(abs(v) for v in self.pos.values())
            self.max_gross = max(self.max_gross, gross)
            self.over += gross > A.GROSS_LIMIT
            if snap['tick'] != last_tick:
                last_tick = snap['tick']
                net = abs(sum(self.pos.values()))
                self.fines += max(0, gross - A.GROSS_LIMIT) + max(0, net - A.GROSS_LIMIT)
                self.path.append((last_tick, self.pnl(snap)))
            for t in TICKERS:
                book = self.books[t]
                if not book.bids or not book.asks:
                    continue
                mine = [(oid, o['a'], o['p'], o['rem']) for oid, o in self.orders.items() if o['t'] == t]
                cancel, now, later = self.strat.decide(t, book.as_dict(), mine, self.pos[t], snap['tick'], T_END,
                                                       snap['wall'], self)
                job = (t, cancel, now, later)
                if self.delay == 0:
                    self.apply(k, 'batch', job)
                else:
                    self.pending.append((k + self.delay, 'batch', job))
        return self.result()

    def apply(self, k, kind, pay):
        """Like algo2e.execute: cancels and `now` orders together; `later` orders only if every cancel on
        the ticker found its order still open (one that already filled means our room was used up)."""
        t, cancel, now, later = pay
        ok = True
        for oid in cancel:
            ok &= self.orders.pop(oid, None) is not None
        for o in now + (later if ok else []):
            self.order(k, t, *o)
        if not ok:
            self.held += len(later)

    def order(self, k, t, a, p, q):
        book = self.books[t]
        crosses = p is None or (a == 'BUY' and book.asks and p >= book.asks[0][0]) or \
            (a == 'SELL' and book.bids and p <= book.bids[0][0])
        if crosses:
            self.take(k, t, a, p, q)
        else:
            self.rest(k, t, a, p, q, live_from=k + 1)

    def pnl(self, snap):
        out = 0.0
        for t in TICKERS:
            b, a = snap[t]['bids'][0][0], snap[t]['asks'][0][0]
            out += self.cash[t] + self.pos[t] * (b + a) / 2 * A.TICK
        return out - self.fines

    def result(self):
        last = self.snaps[-1]
        per = {}
        for t in TICKERS:
            b, a = last[t]['bids'][0][0], last[t]['asks'][0][0]
            per[t] = self.cash[t] + self.pos[t] * (b + a) / 2 * A.TICK
        return dict(total=sum(per.values()) - self.fines, per=per, fines=self.fines, fees=dict(self.fees),
                    vol={t: dict(v) for t, v in self.vol.items()}, max_gross=self.max_gross,
                    end_pos=dict(self.pos), path=self.path, held=self.held, over=self.over)


# =====================================================================================================
# Strategies
# =====================================================================================================
class V3:
    """algo2e.py as it runs live: Signal.update then plan() on the others-only book."""
    name = 'v3'

    def __init__(self, cfg=None):
        self.cfg = cfg or A.CFG
        self.sig = {t: A.Signal(self.cfg[t]['w'], self.cfg[t]['mu_max'], self.cfg[t].get('h')) for t in TICKERS}
        self.caps = A.Caps()
        self.mus = defaultdict(list)
        self.cap_log = defaultdict(list)

    def decide(self, t, book, mine, pos, tick, T, now, sim):
        mu = A.observe(self.sig[t], book, now)
        self.caps.observe(t, book, now)
        self.mus[t].append(mu)
        worst = {u: A.worst_case(sim.pos[u], [(i, o['a'], o['p'], o['rem']) for i, o in sim.orders.items()
                                               if o['t'] == u]) for u in TICKERS}
        saved = A.CFG
        A.CFG = self.cfg                      # Caps reads the configured base caps
        try:
            caps = self.caps.current(worst)
        finally:
            A.CFG = saved
        self.cap_log[t].append(caps[t])
        return A.plan(t, A.sized(self.cfg[t], caps[t]), book, mine, pos, mu, tick, T)


class V2:
    """The v2 notebook's settings and strategy (research/v2_strategy.py), fed a book that includes our orders."""
    name = 'v2'

    def __init__(self):
        ns = {}
        with open(os.path.join(HERE, 'v2_strategy.py')) as f:
            exec(f.read(), ns)
        ns['ME'] = 'ME'
        ns['LAG'].update(dict.fromkeys(TICKERS, 0.0))
        ns['MODE'].update(dict.fromkeys(TICKERS, 'FAST'))
        ns['RACES'].update({t: [0, 0] for t in TICKERS})
        self.ns = ns
        self.last_tick = None
        self.max_oid = 0

    def decide(self, t, book, mine, pos, tick, T, now, sim):
        ns = self.ns
        if tick != self.last_tick:
            self.last_tick = tick
            if tick % ns['PROBE'] == 0:
                ns['LAG'].update(dict.fromkeys(TICKERS, 0.0))
        rows = {'bids': [], 'asks': []}
        for side in ('bids', 'asks'):
            for p, q, who, oid in book[side]:
                self.max_oid = max(self.max_oid, oid)
                rows[side].append({'price': p * A.TICK, 'quantity': q, 'quantity_filled': 0,
                                   'trader_id': who, 'order_id': oid})
        mine_rows = []
        for oid, a, p, rem in mine:
            o = sim.orders[oid]
            o.setdefault('rit_id', self.max_oid + oid)
            r = {'price': p * A.TICK, 'quantity': rem, 'quantity_filled': 0, 'trader_id': 'ME',
                 'order_id': o['rit_id'], 'action': a, 'sim_id': oid}
            mine_rows.append(r)
            rows['bids' if a == 'BUY' else 'asks'].append(r)
        rows['bids'].sort(key=lambda o: (-o['price'], o['order_id']))
        rows['asks'].sort(key=lambda o: (o['price'], o['order_id']))
        cancel, now_, later = ns['plan'](t, rows, mine_rows, pos, tick, T)
        back = {r['order_id']: r['sim_id'] for r in mine_rows}
        return [back[c] for c in cancel if c in back], now_, later


def summary(name, r):
    per = '  '.join(f"{t} {r['per'][t]:+9.0f}" for t in TICKERS)
    vol = '  '.join(f"{t} {sum(r['vol'].get(t, {}).values()) / 1e6:4.1f}M" for t in TICKERS)
    return (f"  {name:10} total {r['total']:+10.0f}   {per}   fines {r['fines']:6.0f}   vol {vol}   "
            f"max gross {r['max_gross']} ({r['over']} snaps over)")


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('paths', nargs='*')
    ap.add_argument('--delay', type=int, default=0)
    args = ap.parse_args()
    for path in args.paths or files():
        snaps = load(path)
        print(f"{os.path.basename(path)}  {len(snaps)} snapshots  ticks {snaps[0]['tick']}-{snaps[-1]['tick']}")
        for strat in (V2(), V3()):
            print(summary(strat.name, Sim(snaps, strat, args.delay).run()))
