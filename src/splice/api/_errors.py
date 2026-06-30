# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations


class DeviceError(Exception):
    """Base exception for all Device API errors."""


class ConnectionError(DeviceError):  # noqa: A001
    """NETCONF connection failure."""


class DiscoveryError(DeviceError):
    """Module discovery or catalog build failure."""


class PathNotFoundError(DeviceError):
    """Requested path is not in the catalog."""


class PathNotWritableError(DeviceError):
    """Path has config=false and cannot be used in set()."""


class SetError(DeviceError):
    """edit-config RPC failure."""


class SubscriptionError(DeviceError):
    """Subscription setup failure."""
