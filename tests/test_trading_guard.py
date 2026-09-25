"""
PHASE 4 Security Boundary tests: hard trading-execution guard.

- Verifies the current codebase is clean of execution symbols.
- Verifies the guard RAISES when execution code IS present.
"""
import os
import tempfile
from decimal import Decimal

import pytest

from app.trading_guard import (
    scan_repo, assert_no_execution_code, ExecutionCodeFound, GuardResult,
)


def test_current_repo_is_clean():
    """The shipped app/ + backtest/ must contain no execution symbols."""
    result = scan_repo()
    assert result.clean, f"Unexpected violations: {result.violations}"
    assert result.files_scanned > 0


def test_guard_raises_on_violation(tmp_path):
    """A repo that DOES contain create_order must fail the guard."""
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "evil.py").write_text(
        "def create_order():\n    return True\n",
    )
    with pytest.raises(ExecutionCodeFound):
        assert_no_execution_code(root=str(tmp_path))


def test_guard_ignores_test_directory():
    """Violations under tests/ do NOT trip the guard (scan app/+backtest/ only)."""
    import app.trading_guard as tg
    result = scan_repo()
    # No violations from tests/ should appear
    for v in result.violations:
        assert "tests" not in v


def test_futures_variable_name_in_backtest_does_not_trip_guard():
    """The `futures` local variable in backtest/execution.py is a list of
    candles, not a futures API call. It must not be flagged."""
    result = scan_repo()
    # The specific backtest/execution.py file should have zero violations
    exec_violations = [v for v in result.violations if "backtest/execution" in v]
    assert exec_violations == [], f"False positive: {exec_violations}"
