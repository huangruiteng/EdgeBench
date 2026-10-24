import math

import pytest

from sforge.harness.score_rescale import RescaleSpec, rescale_score


@pytest.mark.parametrize(
    "spec",
    [
        RescaleSpec(kind="min_linear", baseline=100, expert=10),
        RescaleSpec(kind="min_linear_positive", baseline=100, expert=10),
        RescaleSpec(kind="min_inverse_anchor", anchor_raw=10, anchor_score=50),
        RescaleSpec(kind="compression_ratio_cropped_guarded", baseline=100, expert=10),
        RescaleSpec(kind="log_min", baseline=100, expert=10),
        RescaleSpec(kind="piecewise_min", baseline=100, rank30=50, rank1=20, super_anchor=10),
        RescaleSpec(kind="piecewise_log_min", baseline=100, rank30=50, rank1=20, super_anchor=10),
    ],
)
def test_positive_infinity_is_the_worst_finite_minimization_score(spec):
    assert rescale_score(spec, math.inf) == 0.0


def test_nonfinite_values_without_a_minimization_boundary_remain_unavailable():
    assert rescale_score(RescaleSpec(kind="linear", lower=0, upper=1), math.inf) is None
    assert rescale_score(
        RescaleSpec(kind="piecewise_log_min", baseline=100, rank30=50, rank1=20, super_anchor=10),
        math.nan,
    ) is None
    assert rescale_score(
        RescaleSpec(kind="piecewise_log_min", baseline=100, rank30=50, rank1=20, super_anchor=10),
        -math.inf,
    ) is None
