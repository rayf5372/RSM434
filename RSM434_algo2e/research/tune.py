"""Compare variants of algo2e.CFG on every recorded case, at both execution delays.

    python research/tune.py

Each variant is a function that edits a copy of the config. Reports mean P&L per case, the worst case,
and the live request load the variant implies (reads + cancels + orders per second)."""
import copy
import os
import sys
from concurrent.futures import ProcessPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..'))

import algo2e as A  # noqa: E402
from backtest import V3, Sim  # noqa: E402
from data import TICKERS, files, load  # noqa: E402

_SNAPS = {}
GLOBALS = ('STRICT', 'GAIN_MAX', 'PRIOR', 'CALIB', 'BAND', 'HYST', 'TOPUP', 'WIND', 'CLOSE', 'HORIZON')
DEFAULTS = {k: getattr(A, k) for k in GLOBALS}


class Counted(V3):
    def __init__(self, cfg, glob=None):
        for k, v in {**DEFAULTS, **(glob or {})}.items():
            setattr(A, k, v)
        super().__init__(cfg)
        self.cycles = self.cancels = self.orders = 0
        self.last = None

    def decide(self, t, book, mine, pos, tick, T, now, sim):
        out = super().decide(t, book, mine, pos, tick, T, now, sim)
        if now != self.last:
            self.last = now
            self.cycles += 1
        self.cancels += bool(out[0])
        self.orders += len(out[1]) + len(out[2])
        return out


def one(args):
    path, cfg, glob, delay = args
    if path not in _SNAPS:
        _SNAPS[path] = load(path)
    snaps = _SNAPS[path]
    st = Counted(cfg, glob)
    r = Sim(snaps, st, delay).run()
    dur = snaps[-1]['wall'] - snaps[0]['wall']
    # live cost per cycle: 5 reads, one bulk cancel if any ticker cancels, one request per order
    load_rps = (st.cycles * 5 + st.cancels / 3 + st.orders) / dur
    scale = 300 / max(dur, 1)
    return r['total'] * scale, {t: r['per'][t] * scale for t in TICKERS}, load_rps, r['max_gross']


def evaluate(variants, delays=(0, 1), paths=None):
    paths = paths or files()
    jobs, keys = [], []
    for name, fn in variants:
        cfg = copy.deepcopy(A.CFG)
        glob = fn(cfg) or {}
        for d in delays:
            for p in paths:
                jobs.append((p, cfg, glob, d))
                keys.append((name, d, p))
    with ProcessPoolExecutor() as ex:
        res = list(ex.map(one, jobs, chunksize=1))
    table = {}
    for (name, d, p), r in zip(keys, res):
        table.setdefault((name, d), []).append(r)
    print(f"{len(paths)} cases, P&L scaled to 300 s")
    print(f"{'variant':28} " + ''.join(f"{'delay ' + str(d) + ' mean':>14} {'worst':>9}" for d in delays)
          + '   ' + '  '.join(f'{t:>7}' for t in TICKERS) + '   req/s  max gross')
    for name, _ in variants:
        line = f'{name:28} '
        for d in delays:
            rs = table[(name, d)]
            line += f"{sum(r[0] for r in rs) / len(rs):14,.0f} {min(r[0] for r in rs):9,.0f}"
        rs = table[(name, delays[-1])]
        line += '   ' + '  '.join(f"{sum(r[1][t] for r in rs) / len(rs):7,.0f}" for t in TICKERS)
        line += f"   {max(r[2] for r in rs):5.0f}  {max(r[3] for r in rs):9.0f}"
        print(line, flush=True)


def scale_w(k):
    def f(cfg):
        for t in TICKERS:
            cfg[t]['w'] = {n: v * k for n, v in cfg[t]['w'].items()}
    return f


def only(fn_cfg):
    return fn_cfg


BASE = [
    ('v3 as configured', lambda c: None),
    ('no forecast', scale_w(0.0)),
    ('forecast x0.5', scale_w(0.5)),
    ('forecast x1.5', scale_w(1.5)),
    ('no takes', lambda c: [c[t].update(take=99) for t in TICKERS] and None),
    ('takes at 0.5', lambda c: [c[t].update(take=0.5) for t in TICKERS] and None),
    ('front rung only', lambda c: [c[t].update(rungs=c[t]['rungs'][:1]) for t in TICKERS] and None),
    ('no in-run gain', lambda c: {'GAIN_MAX': 1.0, 'PRIOR': 1e9}),
    ('STRICT room', lambda c: {'STRICT': True}),
]

if __name__ == '__main__':
    evaluate(BASE)
