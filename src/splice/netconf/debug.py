# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional


def _safe_slug(path: str) -> str:
    """Convert a path to a safe directory name."""
    slug = path.replace("/", "__")
    slug = re.sub(r"[^a-zA-Z0-9_-]", "_", slug)
    return slug[:100]


class DiscoveryDebugWriter:
    """Write NETCONF discovery debug information to disk.

    When enabled, writes XML request/response for each discovery attempt
    to help diagnose issues with vendor devices.
    """

    def __init__(self, output_dir: str, enabled: bool = False):
        self.output_dir = Path(output_dir)
        self.enabled = enabled
        if enabled:
            self.output_dir.mkdir(parents=True, exist_ok=True)

    def write_attempt(
        self,
        list_path: str,
        attempt_name: str,
        request_xml: str,
        response_xml: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        """Write request/response XML for a discovery attempt.

        Args:
            list_path: The canonical path being discovered
            attempt_name: Name of the attempt (e.g., "keys_only_get", "shallow_get")
            request_xml: The filter XML sent to the device
            response_xml: The XML response from the device (if successful)
            error: Error message (if failed)
        """
        if not self.enabled:
            return

        path_dir = self.output_dir / _safe_slug(list_path)
        path_dir.mkdir(parents=True, exist_ok=True)

        # Write request
        req_path = path_dir / f"{attempt_name}_request.xml"
        req_path.write_text(request_xml, encoding="utf-8")

        # Write response or error
        if response_xml:
            resp_path = path_dir / f"{attempt_name}_response.xml"
            resp_path.write_text(response_xml, encoding="utf-8")
        if error:
            err_path = path_dir / f"{attempt_name}_error.txt"
            err_path.write_text(error, encoding="utf-8")

    def write_summary(
        self,
        list_path: str,
        status: str,
        attempt_used: Optional[str],
        instance_count: int,
        duration_ms: int,
    ) -> None:
        """Write a summary file for a completed discovery."""
        if not self.enabled:
            return

        path_dir = self.output_dir / _safe_slug(list_path)
        path_dir.mkdir(parents=True, exist_ok=True)

        summary = (
            f"Path: {list_path}\n"
            f"Status: {status}\n"
            f"Attempt Used: {attempt_used or 'none'}\n"
            f"Instances Found: {instance_count}\n"
            f"Duration (ms): {duration_ms}\n"
        )
        summary_path = path_dir / "summary.txt"
        summary_path.write_text(summary, encoding="utf-8")
