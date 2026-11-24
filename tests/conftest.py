import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent))  # tests/scenes.py

import scenes as S  # noqa: E402
from tamper_watch.detect import Thresholds, measure  # noqa: E402


@pytest.fixture(scope="session", params=[1, 2, 3])
def world(request):
    return S.world(request.param)


def learning_frames(w, n=10):
    """What a camera sends while it learns: a little sensor noise, and now and then someone passing."""
    out = []
    for i in range(n):
        f = S.add_noise(S.view(w), 3, seed=100 + i)
        if i % 3 == 0:
            f = S.with_blob(f, 0.10, x=(i + 1) / (n + 1), seed=i)
        out.append(f)
    return out


@pytest.fixture(scope="session")
def learned(world):
    """(reference edge share, normal sharpness) as the add-on learns them."""
    ms = [measure(f, Thresholds()) for f in learning_frames(world)]
    return np.mean([m.edges for m in ms], axis=0), float(np.median([m.sharpness for m in ms]))


@pytest.fixture
def normal(world):
    return S.add_noise(S.view(world), 3, seed=999)
