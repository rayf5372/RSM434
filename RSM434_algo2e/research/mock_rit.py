"""A local stand-in for the RIT DMA REST API, for testing algo2e.main() end to end without the real server.

    python research/mock_rit.py data/<case>.jsonl.gz [--speed 1] [--port 9999]

Replays a recorded case in real time (or faster) and fills our orders with the backtest's queue model.
Serves the endpoints algo2e uses, with RIT's JSON shapes, and RIT's 100 requests/second limit (HTTP 429
with Retry-After). Then point the algo at it:  algo2e.BASE_URL = 'http://127.0.0.1:9999/v1'
"""
import argparse
import json
import os
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..'))

import algo2e as A  # noqa: E402
from backtest import Book, Sim  # noqa: E402
from data import TICKERS, load  # noqa: E402

ME = 'TESTER'
LIMIT_PER_S = 100


class Engine(Sim):
    """The backtest's fill engine, advanced by the wall clock and driven by HTTP requests."""

    def __init__(self, snaps, speed=1.0, latency=0.0):
        super().__init__(snaps, strategy=None)
        self.speed, self.latency = speed, latency
        self.lock = threading.RLock()
        self.k = 0
        self.start = None
        self.meta = {}                       # order id -> dict(ticker, type, qty, action, price, filled, tick)
        self.tx = []
        self.stopped = False
        self.books = {t: Book(snaps[0][t], self.used[t]) for t in TICKERS}
        self.last_tick = None
        self.requests = deque()

    # --- clock -----------------------------------------------------------------------------------------
    def begin(self):
        self.start = time.time()

    def advance(self):
        if self.start is None or self.stopped:
            return
        target = self.snaps[0]['wall'] + (time.time() - self.start) * self.speed
        while self.k + 1 < len(self.snaps) and self.snaps[self.k + 1]['wall'] <= target:
            self.k += 1
            snap = self.snaps[self.k]
            self.books = {t: Book(snap[t], self.used[t]) for t in TICKERS}
            self.passive_fills(self.k, snap)
            if snap['tick'] != self.last_tick:
                self.last_tick = snap['tick']
                gross = sum(abs(v) for v in self.pos.values())
                net = abs(sum(self.pos.values()))
                fine = max(0, gross - A.GROSS_LIMIT) + max(0, net - A.GROSS_LIMIT)
                if fine:
                    self.fines += fine
                    self.tx.append({'ticker': 'LIMIT-STOCK', 'type': 'GROSS_LIMIT_FINE', 'cash_flow': -fine})
        if self.k + 1 >= len(self.snaps):
            self.stopped = True

    def tick(self):
        return self.snaps[self.k]['tick'] if self.start is not None else 0

    # --- fills are mirrored into order metadata -----------------------------------------------------------
    def passive_fills(self, k, snap):
        before = {oid: o['rem'] for oid, o in self.orders.items()}
        super().passive_fills(k, snap)
        for oid, rem in before.items():
            now = self.orders[oid]['rem'] if oid in self.orders else 0
            if now < rem:
                self.meta[oid]['filled'] += rem - now

    # --- API ---------------------------------------------------------------------------------------------
    def post_order(self, t, typ, qty, action, price):
        oid = self.next_id
        p = None if typ == 'MARKET' else round(price / A.TICK)
        self.meta[oid] = dict(ticker=t, type=typ, qty=qty, action=action, price=price, filled=0, tick=self.tick())
        before = self.pos[t]
        book = self.books[t]
        crosses = p is None or (action == 'BUY' and book.asks and p >= book.asks[0][0]) or \
            (action == 'SELL' and book.bids and p <= book.bids[0][0])
        if crosses:                                  # a remainder rests under next_id, which is still oid
            self.take(self.k, t, action, p, qty)
        else:
            self.rest(self.k, t, action, p, qty, live_from=self.k + 1)
        self.next_id = max(self.next_id, oid + 1)
        self.meta[oid]['filled'] += abs(self.pos[t] - before)
        return self.order_json(oid)

    def order_json(self, oid):
        m = self.meta[oid]
        open_ = oid in self.orders
        return {'order_id': oid, 'period': 1, 'tick': m['tick'], 'trader_id': ME, 'ticker': m['ticker'],
                'type': m['type'], 'quantity': m['qty'], 'action': m['action'],
                'price': m['price'] if m['type'] == 'LIMIT' else None, 'quantity_filled': m['filled'],
                'vwap': None, 'status': 'OPEN' if open_ else 'TRANSACTED'}

    def book_json(self, t, limit):
        out = {}
        for side, a in (('bids', 'BUY'), ('asks', 'SELL')):
            rows = [{'order_id': oid, 'trader_id': who, 'price': p * A.TICK, 'quantity': q, 'quantity_filled': 0,
                     'ticker': t, 'action': a, 'type': 'LIMIT', 'status': 'OPEN'}
                    for p, q, who, oid in getattr(self.books[t], side)]
            rows += [dict(self.order_json(oid), price=o['p'] * A.TICK) for oid, o in self.orders.items()
                     if o['t'] == t and o['a'] == a]
            rows.sort(key=lambda r: (-r['price'] if a == 'BUY' else r['price']))
            out[side] = rows[:limit]
        return out

    def securities_json(self):
        snap = self.snaps[self.k]
        out = []
        for t in TICKERS:
            b, a = snap[t]['bids'][0][0], snap[t]['asks'][0][0]
            mid = (b + a) / 2 * A.TICK
            out.append({'ticker': t, 'type': 'STOCK', 'position': float(self.pos[t]), 'last': mid,
                        'bid': b * A.TICK, 'ask': a * A.TICK, 'realized': self.cash[t] + self.pos[t] * mid,
                        'unrealized': 0.0, 'volume': 0.0, 'trading_fee': A.TRADING_FEE[t],
                        'limit_order_rebate': A.LIMIT_REBATE[t], 'max_trade_size': 50000,
                        'api_orders_per_second': 500})
        return out


ENGINE = None


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass

    def reply(self, code, body, headers=None):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def limited(self):
        now = time.time()
        q = ENGINE.requests
        with ENGINE.lock:
            while q and now - q[0] > 1.0:
                q.popleft()
            if len(q) >= LIMIT_PER_S:
                wait = max(0.001, 1.0 - (now - q[0]))
                ENGINE.stats['429'] = ENGINE.stats.get('429', 0) + 1
                return wait
            q.append(now)
        return None

    def handle_any(self, method):
        if ENGINE.latency:
            time.sleep(ENGINE.latency)
        wait = self.limited()
        if wait:
            return self.reply(429, {'code': 'TOO_MANY_REQUESTS', 'message': 'Rate limit of 100 commands/second exceeded',
                                    'wait': round(wait, 3)}, {'Retry-After': f'{wait:.3f}'})
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        path = u.path[len('/v1'):] if u.path.startswith('/v1') else u.path
        with ENGINE.lock:
            ENGINE.advance()
            ENGINE.stats[f'{method} {path}'] = ENGINE.stats.get(f'{method} {path}', 0) + 1
            if method == 'GET' and path == '/case':
                status = 'STOPPED' if ENGINE.stopped else 'ACTIVE'
                return self.reply(200, {'name': 'MOCK ALGO2e', 'period': 1, 'tick': ENGINE.tick(), 'ticks_per_period': 300,
                                        'total_periods': 1, 'status': status, 'is_enforce_trading_limits': False})
            if method == 'GET' and path == '/trader':
                nlv = sum(r['realized'] for r in ENGINE.securities_json()) - ENGINE.fines
                return self.reply(200, {'trader_id': ME, 'first_name': 'Mock', 'last_name': 'Trader', 'nlv': nlv,
                                        'total_fines': ENGINE.fines})
            if method == 'GET' and path == '/securities':
                return self.reply(200, ENGINE.securities_json())
            if method == 'GET' and path == '/securities/book':
                return self.reply(200, ENGINE.book_json(q['ticker'], int(float(q.get('limit', 20)))))
            if method == 'GET' and path == '/orders':
                return self.reply(200, [ENGINE.order_json(oid) for oid in sorted(ENGINE.orders)])
            if method == 'GET' and path in ('/transactions', '/limits'):
                return self.reply(200, ENGINE.tx if path == '/transactions' else [])
            if method == 'POST' and path == '/orders':
                qty = int(float(q['quantity']))
                if qty <= 0 or qty > 50000:
                    return self.reply(400, {'code': 'BAD_QUANTITY'})
                price = float(q['price']) if q.get('price') else None
                if q['type'] == 'LIMIT' and price is None:
                    return self.reply(400, {'code': 'NO_PRICE'})
                return self.reply(200, ENGINE.post_order(q['ticker'], q['type'], qty, q['action'], price))
            if method == 'POST' and path == '/commands/cancel':
                if q.get('all') == '1':
                    ids = list(ENGINE.orders)
                elif 'ticker' in q:
                    ids = [i for i, o in ENGINE.orders.items() if o['t'] == q['ticker']]
                else:
                    ids = [int(x) for x in q.get('ids', '').split(',') if x]
                done = [i for i in ids if ENGINE.orders.pop(i, None) is not None]
                return self.reply(200, {'cancelled_order_ids': done})
        return self.reply(404, {'code': 'NOT_FOUND', 'message': path})

    def do_GET(self):
        self.handle_any('GET')

    def do_POST(self):
        self.handle_any('POST')


def serve(path, port=9999, speed=1.0, latency=0.0, wait_start=True):
    global ENGINE
    ENGINE = Engine(load(path), speed, latency)
    ENGINE.stats = {}
    srv = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    srv.daemon_threads = True
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    return srv, ENGINE


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('path')
    ap.add_argument('--port', type=int, default=9999)
    ap.add_argument('--speed', type=float, default=1.0)
    args = ap.parse_args()
    srv, eng = serve(args.path, args.port, args.speed)
    eng.begin()
    print(f'mock RIT on http://127.0.0.1:{args.port}/v1 replaying {args.path} at {args.speed}x', flush=True)
    try:
        while not eng.stopped:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
