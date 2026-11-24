"""Load recorder files (data/*.jsonl.gz) into per-snapshot rows that the analysis and the backtest share."""
import glob
import gzip
import json
import os

TICKERS = ('CNR', 'RY', 'AC')
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, '..', 'data')


def files(min_snapshots=1500):
    """Recordings long enough to be a full case (~8 snapshots a second for 300 s)."""
    out = []
    for f in sorted(glob.glob(os.path.join(DATA, '*.jsonl.gz'))):
        n = sum(1 for r in read(f) if r['kind'] == 'book')
        if n >= min_snapshots:
            out.append(f)
    return out


def read(path):
    rows = []
    try:
        with gzip.open(path, 'rt') as fh:
            for line in fh:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    break
    except (EOFError, OSError):
        pass
    return rows


def load(path):
    """-> list of snapshots: {'wall', 'tick', t: {'bids', 'asks', 'trades'}} with prices in integer ticks.

    bids/asks: [(price, qty, trader_id, order_id)] best first. trades: [(price, qty)] printed since the
    previous snapshot (the recorder fetches time & sales together with every book)."""
    rows = read(path)
    snaps, seen, first = [], {t: 0 for t in TICKERS}, True
    for r in rows:
        if r['kind'] == 'tas':
            # written right after the book of the same cycle: these prints end at that snapshot.
            # The first poll returns a backlog from before the recording started, so it only sets `seen`.
            if first or not snaps:
                first = False
                for t in TICKERS:
                    seen[t] = max([seen[t]] + [x[0] for x in r.get(t, [])])
                continue
            for t in TICKERS:
                for tid, _tick, p, q in sorted(r.get(t, [])):
                    if tid > seen[t]:
                        seen[t] = tid
                        snaps[-1][t]['trades'].append((round(p * 100), q))
            continue
        if r['kind'] != 'book' or not all(t in r for t in TICKERS):
            continue
        snap = {'wall': r['wall'], 'tick': r['tick']}
        for t in TICKERS:
            snap[t] = {
                'bids': [(round(p * 100), q, who, oid) for p, q, who, oid in r[t]['b'] if q > 0],
                'asks': [(round(p * 100), q, who, oid) for p, q, who, oid in r[t]['a'] if q > 0],
                'trades': [],
            }
        snaps.append(snap)
    return [s for s in snaps if all(s[t]['bids'] and s[t]['asks'] for t in TICKERS)]
