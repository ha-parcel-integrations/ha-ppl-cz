"""The pre-tracking-source import paths must keep resolving.

``api.py``, ``coordinator.py`` and ``parcels.py`` at package root became
re-export shims when the mojePPL account source moved into ``account/``.
Nothing in this repo imports them any more, so only a test notices if a
rename quietly breaks a user's automation or an external import.
"""
from custom_components.ppl_cz import api, coordinator, parcels
from custom_components.ppl_cz.account import api as account_api
from custom_components.ppl_cz.account import coordinator as account_coordinator
from custom_components.ppl_cz.account import parcels as account_parcels


def test_root_coordinator_module_re_exports_the_account_coordinator():
    assert coordinator.PPLCZCoordinator is account_coordinator.PPLCZCoordinator


def test_root_api_module_re_exports_the_account_client():
    assert api.PPLCZApiClient is account_api.PPLCZApiClient
    assert api.PPLCZApiError is account_api.PPLCZApiError
    assert api.PPLCZAuthError is account_api.PPLCZAuthError
    assert api.PPLCZInvalidPin is account_api.PPLCZInvalidPin


def test_root_parcels_module_re_exports_the_account_normaliser():
    assert parcels.normalize_parcel is account_parcels.normalize_parcel
    assert parcels.map_parcel_status is account_parcels.map_parcel_status
