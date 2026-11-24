"""Joint search over the settings the sweeps flagged, ranked by the worst case (delay 1).

    python research/search.py            # cap split and AC alternatives
    python research/search.py random 80  # random search around the current CFG
"""
import random
import sys

from tune import evaluate


def alloc(ry, cnr, ac, ac_rungs=None):
    def f(c):
        c['RY']['cap'], c['CNR']['cap'], c['AC']['cap'] = ry, cnr, ac
        c['RY']['rungs'] = ((1.0, min(5000, ry // 2)), (2.5, min(5000, ry - ry // 2)))
        c['RY']['skew'] = 1.0
        c['CNR']['rungs'] = ((0.6, min(4000, cnr // 2 + 500)), (3.0, min(3000, cnr // 2)))
        c['CNR']['w'] = {n: v * 1.5 for n, v in c['CNR']['w'].items()}
        if ac == 0:
            c['AC']['rungs'] = ()
            c['AC']['take'] = 99
        elif ac_rungs is not None:
            c['AC']['rungs'] = ac_rungs
        else:
            c['AC']['rungs'] = ((1.0, min(4000, ac // 2 + 500)), (2.0, min(3000, ac // 2)))
    return f


ALLOC = [
    ('RY10 CNR7 AC7', alloc(10000, 7000, 7000)),
    ('RY12 CNR7 AC5', alloc(12000, 7000, 5000)),
    ('RY14 CNR7 AC3', alloc(14000, 7000, 3000)),
    ('RY14 CNR10 AC0', alloc(14000, 10000, 0)),
    ('RY16 CNR8 AC0', alloc(16000, 8000, 0)),
    ('RY17 CNR7 AC0', alloc(17000, 7000, 0)),
    ('RY14 CNR7 AC3 take-only', alloc(14000, 7000, 3000, ac_rungs=())),
    ('RY20 CNR4 AC0', alloc(20000, 4000, 0)),
]


def rand_variant(rng):
    """Around the current CFG: RY forecast scale, skew, rung edges and sizes, take threshold; CNR edges."""
    ry_w = rng.choice((1.0, 1.5, 2.0))
    ry_skew = rng.choice((0.5, 0.75, 1.0, 1.5))
    ry_e0 = rng.choice((0.6, 0.8, 1.0, 1.3, 1.6))
    ry_e1 = rng.choice((2.0, 2.5, 3.0, 3.5))
    ry_e2 = rng.choice((None, 4.0, 5.0, 6.0))
    ry_s0 = rng.choice((3000, 4000, 5000))
    ry_take = rng.choice((0.3, 0.6, 1.0, 1.5))
    cnr_e0 = rng.choice((0.3, 0.6, 1.0))
    cnr_skew = rng.choice((1.0, 2.0))
    name = (f'RY w{ry_w} k{ry_skew} e{ry_e0}/{ry_e1}/{ry_e2} s{ry_s0 // 1000} t{ry_take} | '
            f'CNR k{cnr_skew} e{cnr_e0}')

    def f(c):
        c['RY']['w'] = {n: v * ry_w for n, v in c['RY']['w'].items()}
        r = ((ry_e0, ry_s0), (ry_e1, 5000)) + (((ry_e2, 5000),) if ry_e2 else ())
        c['RY'].update(skew=ry_skew, take=ry_take, rungs=r)
        c['CNR'].update(skew=cnr_skew, rungs=((cnr_e0,) + c['CNR']['rungs'][0][1:],) + c['CNR']['rungs'][1:])
    return name, f


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'random':
        rng = random.Random(7)
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 60
        evaluate([('current', lambda c: None)] + [rand_variant(rng) for _ in range(n)], delays=(1,))
    else:
        evaluate(ALLOC)
