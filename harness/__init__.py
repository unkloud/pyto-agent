"""pyto-harness: a stdlib-only LLM agent harness that runs inside Pyto on iOS.

The package is written for CPython 3.10 and imports nothing outside the standard
library.  iOS-specific functionality lives in :mod:`harness.ios` and degrades to a
recorded "unsupported" result on any other platform, so the whole thing is testable
offline on Linux.

Layering (no cycles)::

    errors / schema / textbudget   <- leaf helpers
    home                           <- the one resolver for ~/pyto_harness (no literal '~')
    config / session               <- persistence and settings
    llm                            <- provider transport
    tools                          <- registry + dispatch
    ios                            <- device capability adapters
    tools_ios                      <- the model-facing tool set
    loop                           <- agent loop (uses everything above)
    doctor                         <- self-diagnosis checks and the fixes a machine can apply
    repair                         <- snapshot-first, test-gated edits of this source
    ui / run                       <- front ends

``doctor`` is read-only except when a fix is explicitly applied; ``repair`` writes source
files and is the only module allowed to, gated by the offline test suite.
"""

__version__ = "1.0.16"
__all__ = ["__version__"]
