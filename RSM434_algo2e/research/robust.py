"""Is the chosen config on a knife-edge? Perturb each RY setting and see how much P&L moves.

    python research/robust.py
"""
from tune import evaluate


def wscale(k):
    def f(c):
        c['RY']['w'] = {n: v * k for n, v in c['RY']['w'].items()}
    return f


def rung_shift(d):
    def f(c):
        c['RY']['rungs'] = tuple((max(0.2, e + d * i), s) for i, (e, s) in enumerate(c['RY']['rungs']))
    return f


V = [
    ('as configured', lambda c: None),
    ('RY forecast x0.5', wscale(0.5)),
    ('RY forecast x0.75', wscale(0.75)),
    ('RY forecast x1.33', wscale(1.33)),
    ('RY skew 0.5', lambda c: c['RY'].update(skew=0.5)),
    ('RY skew 2', lambda c: c['RY'].update(skew=2.0)),
    ('RY rungs tighter', rung_shift(-0.4)),
    ('RY rungs wider', rung_shift(0.4)),
    ('RY take 0.3', lambda c: c['RY'].update(take=0.3)),
    ('RY take 1.5', lambda c: c['RY'].update(take=1.5)),
    ('RY cap 16000', lambda c: c['RY'].update(cap=16000)),
    ('no in-run gain', lambda c: {'GAIN_MAX': 1.0, 'PRIOR': 1e9}),
    ('CNR off, RY 24000', lambda c: [c['CNR'].update(cap=0), c['RY'].update(cap=24000)] and None),
]

if __name__ == '__main__':
    evaluate(V, delays=(1,))
