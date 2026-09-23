"""Compatibility module for the mojePPL account normaliser."""
from .account import parcels as _account

globals().update(vars(_account))
