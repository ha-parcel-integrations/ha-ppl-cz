"""Compatibility module for the mojePPL account API client.

New code imports from :mod:`ppl_cz.account.api`; this module preserves the
pre-tracking-source public import path (including its private helpers, which
older tests still reach into) for custom automations and older tests.
"""
from .account import api as _account

globals().update(vars(_account))
