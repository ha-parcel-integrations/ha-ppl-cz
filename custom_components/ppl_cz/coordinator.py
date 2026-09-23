"""Compatibility module for the mojePPL account coordinator."""
from .account import coordinator as _account

globals().update(vars(_account))
