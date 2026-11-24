"""Microstructure stats from recorder files: spreads, flow, and how predictable the mid is at each horizon.

    python research/analyze.py            # every full recording in data/
"""
import math
import statistics as st
import sys

from data import TICKERS, files, load

GRID = 0.25   # seconds


def corr(x, y):
    if len(x) < 3:
        return float('nan')
    mx, my = st.fmean(x), st.fmean(y)
    sx = math.sqrt(sum((a - mx) ** 2 for a in x))
    sy = math.sqrt(sum((b - my) ** 2 for b in y))
    return sum((a - mx) * (b - my) for a, b in zip(x, y)) / (sx * sy) if sx and sy else float('nan')


def ols(X, y):
    """Least squares with an intercept. Returns (coefs, r2)."""
    n, k = len(X), len(X[0]) + 1
    A = [[1.0] + list(r) for r in X]
    ata = [[sum(A[i][a] * A[i][b] for i in range(n)) for b in range(k)] for a in range(k)]
    aty = [sum(A[i][a] * y[i] for i in range(n)) for a in range(k)]
    for c in range(k):                                   # Gauss-Jordan
        piv = max(range(c, k), key=lambda r: abs(ata[r][c]))
        ata[c], ata[piv], aty[c], aty[piv] = ata[piv], ata[c], aty[piv], aty[c]
        d = ata[c][c] or 1e-12
        for r in range(k):
            if r != c:
                f = ata[r][c] / d
                ata[r] = [a - f * b for a, b in zip(ata[r], ata[c])]
                aty[r] -= f * aty[c]
    beta = [aty[i] / (ata[i][i] or 1e-12) for i in range(k)]
    pred = [sum(b * a for b, a in zip(beta, row)) for row in A]
    my = st.fmean(y)
    ss = sum((v - my) ** 2 for v in y) or 1e-12
    return beta, 1 - sum((v - p) ** 2 for v, p in zip(y, pred)) / ss


def series(snaps, t):
    """Regular grid of (mid, spread, anon_mid, micro) from the last snapshot at or before each point."""
    t0, t1 = snaps[0]['wall'], snaps[-1]['wall']
    out, j = [], 0
    g = t0
    while g <= t1:
        while j + 1 < len(snaps) and snaps[j + 1]['wall'] <= g:
            j += 1
        b = snaps[j][t]
        bb, ba = b['bids'][0][0], b['asks'][0][0]
        bq = sum(q for p, q, *_ in b['bids'] if p == bb)
        aq = sum(q for p, q, *_ in b['asks'] if p == ba)
        ab = next((p for p, q, who, _ in b['bids'] if who == 'ANON'), bb)
        aa = next((p for p, q, who, _ in b['asks'] if who == 'ANON'), ba)
        out.append(((bb + ba) / 2, ba - bb, (ab + aa) / 2, (bb * aq + ba * bq) / (bq + aq)))
        g += GRID
    return out


def ema(xs, tau):
    a = 1 - math.exp(-GRID / tau)
    out, m = [], xs[0]
    for x in xs:
        m += a * (x - m)
        out.append(m)
    return out


def report(path):
    snaps = load(path)
    dur = snaps[-1]['wall'] - snaps[0]['wall']
    print(f'\n##### {path.split("/")[-1]}   {len(snaps)} snapshots over {dur:.0f}s   ticks {snaps[0]["tick"]}-{snaps[-1]["tick"]}')
    res = {}
    for t in TICKERS:
        s = series(snaps, t)
        mid = [r[0] for r in s]
        spr = [r[1] for r in s]
        vol = sum(q for sn in snaps for _, q in sn[t]['trades'])
        rivals = set(who for sn in snaps[::20] for side in ('bids', 'asks') for _, _, who, _ in sn[t][side][:10]
                     if who != 'ANON')
        top_rival = sum(1 for sn in snaps if sn[t]['bids'][0][2] != 'ANON') / len(snaps)
        print(f'== {t}  mid {mid[0] / 100:.2f} -> {mid[-1] / 100:.2f}   spread median {st.median(spr):.0f} '
              f'mean {st.fmean(spr):.2f} ticks   anon spread median '
              f'{st.median([2 * abs(r[2] - r[0]) + r[1] for r in s]):.0f}   volume/s {vol / dur:,.0f}   '
              f'rivals in top 10 {len(rivals)}   best bid is a rival {top_rival:.0%}')
        step = round(1 / GRID)
        r1 = [mid[i] - mid[i - step] for i in range(step, len(mid))]
        v1 = st.pvariance(r1)
        line = '   VR '
        for h in (0.5, 1, 2, 5, 10, 20, 40):
            k = max(1, round(h / GRID))
            rk = [mid[i] - mid[i - k] for i in range(k, len(mid))]
            line += f' {h:g}s:{st.pvariance(rk) / (v1 * h):.2f}'
        print(line + f'   (1s sd {math.sqrt(v1):.2f} ticks)')
        line = '   corr(past h, next h)'
        for h in (0.5, 1, 2, 5, 10, 20):
            k = max(1, round(h / GRID))
            idx = range(k, len(mid) - k)
            line += f' {h:g}s:{corr([mid[i] - mid[i - k] for i in idx], [mid[i + k] - mid[i] for i in idx]):+.2f}'
        print(line)
        # predictors of the next H seconds
        feats = {
            'rev2': [a - b for a, b in zip(ema(mid, 2), mid)],
            'rev8': [a - b for a, b in zip(ema(mid, 8), mid)],
            'rev30': [a - b for a, b in zip(ema(mid, 30), mid)],
            'mom': [a - b for a, b in zip(ema(mid, 1), ema(mid, 8))],
            'imb': [r[3] - r[0] for r in s],
            'anon': [r[2] - r[0] for r in s],
        }
        for H in (1, 3, 5):
            k = round(H / GRID)
            idx = list(range(round(30 / GRID), len(mid) - k, 2))
            y = [mid[i + k] - mid[i] for i in idx]
            cs = {n: corr([f[i] for i in idx], y) for n, f in feats.items()}
            names = list(feats)
            beta, r2 = ols([[feats[n][i] for n in names] for i in idx], y)
            res[(t, H)] = dict(zip(['c'] + names, beta))
            print(f'   next {H}s: corr ' + ' '.join(f'{n}:{c:+.2f}' for n, c in cs.items())
                  + f'   | OLS R2 {r2:.2f} ' + ' '.join(f'{n}:{b:+.2f}' for n, b in zip(names, beta[1:])))
    return res


if __name__ == '__main__':
    paths = sys.argv[1:] or files()
    for p in paths:
        report(p)
