"""
PHASE 4 Security Boundary: Hard Trading-Execution Guard.

Scans the repository for known trading-execution symbols and raises a
hard error if any are found in app/ or backtest/, so future execution
functionality cannot be accidentally enabled from this application.

Enforced by:
- app.main calls assert_no_execution_code() at startup
- tests/test_trading_guard.py verifies the scan itself

This is a SIGNAL-ONLY application. No order creation, no API-key
signing, no futures API calls.
"""
import os
import re
from dataclasses import dataclass, field
from typing import List

# Words that indicate real trading execution. Any occurrence in app/ or
# backtest/ Python files triggers the guard. The guard's own source file
# is excluded from scanning to avoid matching its own documentation.
_EXECUTION_PATTERNS = [
    r"\bcreate_order\b",
    r"\bnew_order\b",
    r"\bplace_order\b",
    r"\bsubmit_order\b",
    r"\bpost_order\b",
    r"\bcancel_order\b",
    r"\bcancel_orders\b",
    r"\bclose_position\b",
    r"\bfutures_\w+",
    r"\bmargin_\w+",
    r"\bleverage_\w+",
    r"\bwithdraw\w*",
    r"\btransfer\w*",
    r"\bapi_key\b",
    r"\bsecret_key\b",
    r"\bprivate_key\b",
]

# Directories to scan (relative to repo root)
_SCAN_DIRS = ["app", "backtest"]

# This file is excluded from its own scan to avoid docstring self-matches.
_EXCLUDE_SELF = os.path.basename(__file__)


class ExecutionCodeFound(Exception):
    """Raised when a trading-execution symbol is detected in the codebase."""
    pass


@dataclass
class GuardResult:
    files_scanned: int
    violations: List[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return len(self.violations) == 0


def _find_python_files(root: str, dirs: List[str]) -> List[str]:
    """Recursively find all .py files in the given directories."""
    out = []
    for d in dirs:
        dir_path = os.path.join(root, d)
        if not os.path.isdir(dir_path):
            continue
        for dirpath, _dirnames, filenames in os.walk(dir_path):
            for fname in filenames:
                if fname.endswith(".py") and fname != _EXCLUDE_SELF:
                    out.append(os.path.join(dirpath, fname))
    return out


def scan_repo(root: str = None) -> GuardResult:
    """
    Scan app/ and backtest/ Python files for execution keywords.
    Returns a GuardResult with any violations found.
    """
    if root is None:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    compiled = [re.compile(p, re.IGNORECASE) for p in _EXECUTION_PATTERNS]
    files = _find_python_files(root, _SCAN_DIRS)

    violations = []
    for fpath in files:
        try:
            with open(fpath, encoding="utf-8", errors="replace") as fh:
                content = fh.read()
        except OSError:
            continue
        for line_no, line in enumerate(content.splitlines(), start=1):
            for pat in compiled:
                m = pat.search(line)
                if m:
                    violations.append(
                        f"{fpath}:{line_no}: '{m.group(0)}' in: {line.strip()[:80]}"
                    )
    return GuardResult(files_scanned=len(files), violations=violations)


def assert_no_execution_code(root: str = None) -> GuardResult:
    """
    Scan the repo and raise ExecutionCodeFound if any execution symbol is
    present in app/ or backtest/. Returns the GuardResult on success.
    """
    result = scan_repo(root)
    if not result.clean:
        details = "\n".join(result.violations)
        raise ExecutionCodeFound(
            f"Trading-execution code detected in {len(result.violations)} location(s):\n{details}"
        )
    return result
