# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json
import os
import threading
from typing import Iterable


_LOCK = threading.Lock()


def archive_to_file(path: str, updates: Iterable[dict]) -> None:
    dir_path = os.path.dirname(path)
    if dir_path:
        os.makedirs(dir_path, exist_ok=True)
    with _LOCK:
        with open(path, "a", encoding="utf-8") as f:
            for u in updates:
                json.dump(u, f)
                f.write("\n")
