"""Per-ticker parameter sweeps (tickers only interact through the caps, which stay fixed here).

    python research/sweep.py RY        # or CNR, AC
"""
import sys

from tune import TICKERS, evaluate


def set_(t, **kw):
    def f(cfg):
        cfg[t].update(kw)
    return f


def rungs(t, *r):
    return set_(t, rungs=tuple(r))


def wscale(t, k):
    def f(cfg):
        cfg[t]['w'] = {n: v * k for n, v in cfg[t]['w'].items()}
    return f


def grid(t):
    v = [('base', lambda c: None)]
    for k in (0.5, 1.5, 2.0):
        v.append((f'w x{k}', wscale(t, k)))
    for s in (1.0, 3.0, 4.0):
        v.append((f'skew {s}', set_(t, skew=s)))
    for tk in (0.3, 0.6, 1.5):
        v.append((f'take {tk}', set_(t, take=tk)))
    for e0 in (0.0, 0.6, 1.0):
        v.append((f'front edge {e0}', lambda c, e0=e0: c[t].update(rungs=((e0,) + c[t]['rungs'][0][1:],) + c[t]['rungs'][1:])))
    for e1 in (1.0, 3.0, 4.0):
        v.append((f'deep edge {e1}', lambda c, e1=e1: c[t].update(rungs=c[t]['rungs'][:1] + ((e1, c[t]['rungs'][1][1]),))))
    v.append(('3 rungs', lambda c: c[t].update(rungs=c[t]['rungs'] + ((c[t]['rungs'][1][0] + 2.5, 3000),))))
    v.append(('mu_max 2', set_(t, mu_max=2.0)))
    v.append(('mu_max 8', set_(t, mu_max=8.0)))
    return v


if __name__ == '__main__':
    t = sys.argv[1] if len(sys.argv) > 1 else 'RY'
    assert t in TICKERS
    print(f'sweeping {t}')
    evaluate(grid(t))
