# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Splice TUI — interactive management console."""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="splice-tui",
        description="Splice TUI — interactive device management console",
    )
    parser.add_argument(
        "--devices-dir",
        default="devices",
        help="Path to the devices directory (default: devices/)",
    )
    args = parser.parse_args(argv)

    try:
        from textual.app import App  # noqa: F401
    except ImportError:
        print(
            "The TUI requires the 'textual' package.\n"
            "Install it with:  pip install splice[tui]"
        )
        return 1

    from splice.tui.app import SpliceApp

    app = SpliceApp(devices_dir=args.devices_dir)
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
