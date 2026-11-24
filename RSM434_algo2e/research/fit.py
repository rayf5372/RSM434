"""Fit the forecast weights in algo2e.CFG[t]['w'] on recorded cases, scored out of sample.

    python research/fit.py

Features are computed exactly as algo2e.Signal computes them live, at every snapshot. The target is the
mid change over algo2e.HORIZON seconds. Each candidate model is fitted on all cases but one and scored
on the one left out, so a feature only survives if it predicts cases it has not seen."""
import itertools
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..'))

import algo2e as A  # noqa: E402
from data import TICKERS, files, load  # noqa: E402


def frame(snaps, t):
    sig = A.Signal({}, 0)
    rows, times, mids = [], [], []
    for s in snaps:
        b = s[t]
        bb, ba, bq, aq = A.touch(b)
        mid = (bb + ba) / 2
        sig.update(s['wall'], mid, (bb * aq + ba * bq) / (bq + aq))
        rows.append(sig.features(mid, (bb * aq + ba * bq) / (bq + aq), A.anon_mid(b, mid)))
        sig.pending.clear()
        times.append(s['wall'])
        mids.append(mid)
    times, mids = np.array(times), np.array(mids)
    j = np.searchsorted(times, times + A.HORIZON)
    ok = (j < len(times)) & (times - times[0] > 20)
    y = np.where(ok, mids[np.minimum(j, len(mids) - 1)] - mids, 0.0)
    names = list(rows[0])
    X = np.array([[r[n] for n in names] for r in rows])
    return names, X[ok], y[ok]


def r2(y, p):
    return 1 - ((y - p) ** 2).sum() / ((y - y.mean()) ** 2).sum()


def main():
    paths = files()
    print(f'{len(paths)} full cases, horizon {A.HORIZON}s')
    data = {t: [frame(load(p), t) for p in paths] for t in TICKERS}
    for t in TICKERS:
        names = data[t][0][0]
        print(f'\n== {t}')
        results = []
        for k in (1, 2, 3):
            for combo in itertools.combinations(range(len(names)), k):
                oos, coefs = [], []
                for leave in range(len(paths)):
                    Xtr = np.vstack([d[1][:, combo] for i, d in enumerate(data[t]) if i != leave])
                    ytr = np.concatenate([d[2] for i, d in enumerate(data[t]) if i != leave])
                    w, *_ = np.linalg.lstsq(Xtr, ytr, rcond=None)      # no intercept: forecasts are relative
                    Xte, yte = data[t][leave][1][:, combo], data[t][leave][2]
                    oos.append(r2(yte, Xte @ w))
                    coefs.append(w)
                Xall = np.vstack([d[1][:, combo] for d in data[t]])
                yall = np.concatenate([d[2] for d in data[t]])
                w, *_ = np.linalg.lstsq(Xall, yall, rcond=None)
                results.append((np.mean(oos), min(oos), [names[i] for i in combo], w, np.std(coefs, axis=0)))
        results.sort(key=lambda r: -r[0])
        for mean, worst, nm, w, sd in results[:8]:
            print(f'  OOS R2 mean {mean:+.3f} worst {worst:+.3f}   '
                  + '  '.join(f'{n} {c:+.3f}(±{s:.2f})' for n, c, s in zip(nm, w, sd)))


if __name__ == '__main__':
    main()
