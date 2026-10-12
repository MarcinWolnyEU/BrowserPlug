"""Stdlib-only code that both the launcher and the server import.

Importing the package imports nothing from it. tests/unit/test_import_boundaries.py fails if a
module here imports anything but the standard library or this package.
"""
