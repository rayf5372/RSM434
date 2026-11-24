"""Run algo2e.main(), unmodified, against the mock RIT server replaying a recorded case.

    python research/e2e.py [data/<case>.jsonl.gz] [--speed 1]

Checks the live code path (REST calls, order bookkeeping, rate limiting, end-of-case handling) and
compares the exchange-side result with the backtest of the same case."""
import argparse
import os
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..'))

import algo2e as A  # noqa: E402
import mock_rit  # noqa: E402
from data import TICKERS, files  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('path', nargs='?')
    ap.add_argument('--speed', type=float, default=1.0)
    ap.add_argument('--port', type=int, default=9931)
    ap.add_argument('--latency', type=float, default=0.004, help='seconds added to every request')
    args = ap.parse_args()
    path = args.path or files()[-1]
    srv, eng = mock_rit.serve(path, args.port, args.speed, args.latency)
    A.BASE_URL = f'http://127.0.0.1:{args.port}/v1'
    os.chdir(tempfile.mkdtemp(prefix='algo2e-e2e-'))
    print(f'replaying {os.path.basename(path)} at {args.speed}x in {os.getcwd()}', flush=True)

    max_gross = [0]
    stop = threading.Event()

    def watch():
        while not stop.is_set():
            with eng.lock:
                max_gross[0] = max(max_gross[0], sum(abs(v) for v in eng.pos.values()))
            time.sleep(0.01)

    threading.Thread(target=watch, daemon=True).start()
    eng.begin()
    t0 = time.time()
    A.main()
    stop.set()
    with eng.lock:
        snap = eng.snaps[eng.k]
        pnl = {t: eng.cash[t] + eng.pos[t] * (snap[t]['bids'][0][0] + snap[t]['asks'][0][0]) / 2 * A.TICK
               for t in TICKERS}
        print('\n==== exchange side')
        print(f"wall {time.time() - t0:.0f}s  P&L {sum(pnl.values()) - eng.fines:+,.0f}  "
              + '  '.join(f'{t} {v:+,.0f}' for t, v in pnl.items())
              + f"  fines {eng.fines:,.0f}  max gross {max_gross[0]}  end pos {dict(eng.pos)}  open orders {len(eng.orders)}")
        vol = {t: {k: int(v) for k, v in eng.vol[t].items()} for t in TICKERS}
        print(f"volume {vol}")
        print('requests', {k: v for k, v in sorted(eng.stats.items())})
    srv.shutdown()


if __name__ == '__main__':
    main()
