"""Read-only market recorder for ALGO2e. Sends no orders.

Polls the case, all three order books and time & sales, and writes one JSON line per sample to
data/<case start>.jsonl.gz, so strategy parameters can be calibrated on real practice-server data.

    python recorder.py            # record until Ctrl+C
    python recorder.py 1800       # record for 30 minutes
"""
import base64
import gzip
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import requests

BASE_URL = os.environ.get('RIT_URL', 'http://flserver.rotman.utoronto.ca:16585/v1')
USERNAME = os.environ.get('RIT_USER', '1010264928')
PASSWORD = os.environ.get('RIT_PASS', 'fa')
TICKERS = ('CNR', 'RY', 'AC')
BOOK_EVERY = 0.12     # seconds between book + time & sales polls (6 requests)
TAS_EVERY = 0.0       # seconds between time & sales polls; 0 = with every book poll
DEPTH = 60

s = requests.Session()
s.headers['Authorization'] = 'Basic ' + base64.b64encode(f'{USERNAME}:{PASSWORD}'.encode()).decode()
POOL = ThreadPoolExecutor(8)


def get(path, **params):
    try:
        r = s.get(f'{BASE_URL}/{path}', params=params, timeout=2)
        if r.status_code == 429:
            time.sleep(float(r.headers.get('Retry-After') or 0.2))
            return None
        return r.json() if r.ok else None
    except (requests.RequestException, ValueError):
        return None


def compact(side):
    return [[o['price'], o['quantity'] - o['quantity_filled'], o['trader_id'], o['order_id']] for o in side]


def record(seconds):
    stop = time.time() + seconds
    out, period_key, last_tas, last_tick, flushed = None, None, {t: 0 for t in TICKERS}, 0, -1
    next_tas = 0.0
    os.makedirs('data', exist_ok=True)
    while time.time() < stop:
        t0 = time.perf_counter()
        case = get('case')
        if not case or case['status'] != 'ACTIVE':
            if out:
                for t in TICKERS:
                    out.write(json.dumps({'kind': 'history', 'ticker': t,
                                          'rows': get('securities/history', ticker=t, limit=400)}) + '\n')
                out.close()
                print('case finished, file closed', flush=True)
                out, period_key = None, None
            time.sleep(0.5)
            continue
        key = (case['name'], case['period'])
        if out is not None and (key != period_key or case['tick'] < last_tick):
            out.close()
            out = None
        last_tick = case['tick']
        if out is None:
            name = os.path.join('data', time.strftime('%Y%m%d-%H%M%S') + f"-t{case['tick']}.jsonl.gz")
            out, period_key = gzip.open(name, 'wt'), key
            last_tas = {t: 0 for t in TICKERS}
            print('recording', name, flush=True)
        futs = {t: POOL.submit(get, 'securities/book', ticker=t, limit=DEPTH) for t in TICKERS}
        want_tas = t0 >= next_tas
        if want_tas:
            tfuts = {t: POOL.submit(get, 'securities/tas', ticker=t, after=last_tas[t], limit=1000) for t in TICKERS}
            sfut = POOL.submit(get, 'securities')
            next_tas = t0 + TAS_EVERY
        row = {'kind': 'book', 'wall': time.time(), 'tick': case['tick']}
        for t, f in futs.items():
            b = f.result()
            if b:
                row[t] = {'b': compact(b['bids']), 'a': compact(b['asks'])}
        out.write(json.dumps(row) + '\n')
        if want_tas:
            trow = {'kind': 'tas', 'wall': time.time(), 'tick': case['tick']}
            for t, f in tfuts.items():
                rows = f.result() or []
                if rows:
                    last_tas[t] = max(last_tas[t], max(r['id'] for r in rows))
                trow[t] = [[r['id'], r['tick'], r['price'], r['quantity']] for r in rows]
            sec = sfut.result() or []
            trow['sec'] = {d['ticker']: [d.get('last'), d.get('bid'), d.get('ask'), d.get('volume')] for d in sec}
            out.write(json.dumps(trow) + '\n')
        if case['tick'] % 10 == 0 and case['tick'] != flushed:
            flushed = case['tick']
            out.flush()
            print(f"tick {case['tick']}", flush=True)
        time.sleep(max(0.0, BOOK_EVERY - (time.perf_counter() - t0)))
    if out:
        out.close()


if __name__ == '__main__':
    record(float(sys.argv[1]) if len(sys.argv) > 1 else 1e9)
