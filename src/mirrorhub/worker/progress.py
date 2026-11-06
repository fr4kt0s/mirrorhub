"""Progress parsing from tool output.

rsync --info=progress2 prints lines such as "  1,234,567  45%   12.34MB/s  0:00:05",
so a percentage can be extracted reliably. debmirror's --progress output has no stable
percentage, so it stays indeterminate and only the latest line is reported.
"""

from __future__ import annotations

import re

PERCENT_RE = re.compile(r"(?<!\d)(\d{1,3})%")


class ProgressParser:
    def __init__(self) -> None:
        self.pct: float | None = None
        self.text: str = ""

    def feed(self, line: str) -> tuple[float | None, str]:
        text = line.strip()
        if not text:
            return self.pct, self.text
        match = PERCENT_RE.search(text)
        if match:
            value = min(int(match.group(1)), 100)
            self.pct = float(value)
        self.text = text[:300]
        return self.pct, self.text
