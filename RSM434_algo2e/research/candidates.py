"""Head-to-head of the final candidate configs (and v2) on every recording, both delays.

    python research/candidates.py
"""
from tune import evaluate


def mk(w, k, rungs, take, cnr_e0=1.0):
    def f(c):
        c['RY']['w'] = {n: v * w for n, v in c['RY']['w'].items()}
        c['RY'].update(skew=k, take=take, rungs=rungs)
        c['CNR'].update(rungs=((cnr_e0, 2500), (3.0, 2000)))
    return f


CANDIDATES = [
    ('as configured', lambda c: None),
    ('central w1.5 k1 .8/2/4 t.6', mk(1.5, 1.0, ((0.8, 5000), (2.0, 5000), (4.0, 5000)), 0.6)),
    ('central w1.5 k1 .8/2/4.5 t1', mk(1.5, 1.0, ((0.8, 5000), (2.0, 5000), (4.5, 5000)), 1.0)),
    ('top1 w2 k1.5 .6/2/4 t.3', mk(2.0, 1.5, ((0.6, 5000), (2.0, 5000), (4.0, 5000)), 0.3)),
    ('top2 w1 k1 .8/2/5 t.3', mk(1.0, 1.0, ((0.8, 5000), (2.0, 5000), (5.0, 5000)), 0.3)),
    ('central 4 rungs', mk(1.5, 1.0, ((0.8, 5000), (2.0, 5000), (3.5, 5000), (5.5, 5000)), 0.6)),
]

if __name__ == '__main__':
    evaluate(CANDIDATES)
