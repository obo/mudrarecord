"""mudrarecord - Mudra Band Recorder.

Fast, lightweight recording of the Mudra Band's raw channels to CSV or LSL,
with exact global nanosecond timestamps. Fully independent package.
"""
from __future__ import annotations

__version__ = "0.1.0"

from .device import MudraBand

__all__ = ["MudraBand", "__version__"]
