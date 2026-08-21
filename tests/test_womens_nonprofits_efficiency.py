"""TDD (red phase) for specifications/womens-nonprofits-directory.md,
efficiency_ratio NULL handling.

Expected contract for `womens_nonprofits_pipeline` (repo root):

    compute_efficiency_ratio(program_expenses, total_revenue) -> float | None
        program_expenses / total_revenue, NULL-safe:
          - either input is None -> None
          - total_revenue == 0 -> None (never raise ZeroDivisionError)
          - total_revenue < 0 -> None (bad/unreported data, not a ratio)
          - otherwise -> float(program_expenses) / float(total_revenue)

Every test here fails at collection (ModuleNotFoundError) until the module
exists -- expected RED state.
"""
import pytest

from womens_nonprofits_pipeline import compute_efficiency_ratio


def test_normal_ratio():
    assert compute_efficiency_ratio(94800000.0, 105600000.0) == pytest.approx(0.8977, abs=1e-4)


def test_zero_revenue_returns_none_not_zerodivisionerror():
    assert compute_efficiency_ratio(1000.0, 0) is None


def test_none_total_revenue_returns_none():
    assert compute_efficiency_ratio(1000.0, None) is None


def test_none_program_expenses_returns_none():
    assert compute_efficiency_ratio(None, 1000.0) is None


def test_both_none_returns_none():
    assert compute_efficiency_ratio(None, None) is None


def test_negative_revenue_returns_none():
    assert compute_efficiency_ratio(1000.0, -500.0) is None


def test_zero_program_expenses_is_a_valid_zero_ratio():
    """Zero expenses with positive revenue is a real (if suspicious) ratio,
    not a NULL case -- only the denominator being unusable should NULL out."""
    assert compute_efficiency_ratio(0.0, 1000.0) == 0.0
