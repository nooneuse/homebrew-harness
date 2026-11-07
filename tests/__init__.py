"""Test only the source tree containing these tests."""

from pathlib import Path
import sys

_source = Path(__file__).resolve().parent.parent / "src"
_loaded = sys.modules.get("homebrew_harness")
if (
    _loaded is not None
    and Path(_loaded.__file__).resolve().parent != _source / "homebrew_harness"
):
    raise ImportError(
        "The tests must use this workspace source, not another installed checkout."
    )
sys.path.insert(0, str(_source))
