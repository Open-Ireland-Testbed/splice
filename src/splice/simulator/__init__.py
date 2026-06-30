# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Simulation mode for splice.

Provides simulated NETCONF backends backed by device profiles, allowing
the full pipeline (connect -> discover -> get -> set) to run without
real hardware.

Usage (Python API)::

    from splice.api import Device

    dev = Device("sim://openroadm")
    dev.connect()
    data = dev.get("/org-openroadm-device/org-openroadm-device")
    dev.close()

Usage (CLI)::

    splice --simulate openroadm --config config.yaml run
"""
from __future__ import annotations

from typing import Dict, Type

from splice.simulator.backend import SimulatedNetconfClient
from splice.simulator.profiles.base import DeviceProfile
from splice.simulator.profiles.openroadm import OpenROADMProfile

# Registry of available profiles
_PROFILES: Dict[str, Type] = {
    "openroadm": OpenROADMProfile,
}


def get_profile(name: str) -> DeviceProfile:
    """Return an instance of the named profile.

    Raises ``ValueError`` if the profile name is not registered.
    """
    cls = _PROFILES.get(name.lower())
    if cls is None:
        available = ", ".join(sorted(_PROFILES))
        raise ValueError(
            f"unknown simulator profile {name!r}; available: {available}"
        )
    return cls()


def make_simulated_client(profile_name: str) -> SimulatedNetconfClient:
    """Convenience: create a SimulatedNetconfClient for the given profile."""
    return SimulatedNetconfClient(get_profile(profile_name))


def available_profiles() -> list[str]:
    """Return the list of registered profile names."""
    return sorted(_PROFILES)


__all__ = [
    "SimulatedNetconfClient",
    "DeviceProfile",
    "OpenROADMProfile",
    "get_profile",
    "make_simulated_client",
    "available_profiles",
]
