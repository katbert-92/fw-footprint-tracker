"""Track embedded firmware memory footprint across builds."""

from .cli import run as track_build

__version__ = "0.1.0"
__all__ = ["track_build"]
