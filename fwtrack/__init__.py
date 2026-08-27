"""Track embedded firmware memory footprint across builds."""

from importlib.metadata import PackageNotFoundError, version

# Before the import below: db and gen_dashboard read __version__ back out of
# this module, and an import that runs first would find it missing.
try:
    __version__ = version("fw-footprint-tracker")
except PackageNotFoundError:
    # Imported from a source checkout that was never pip-installed.
    __version__ = "0+unknown"

from .cli import run as track_build  # noqa: E402

__all__ = ["__version__", "track_build"]
