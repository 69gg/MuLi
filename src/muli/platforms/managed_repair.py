"""Read-only repair diagnosis, available before importing a selected slot.

The copied standalone launcher exposes the same ``repair`` operation. Actual
recovery must use the retained controller and the one authoritative journal;
this inspector does not re-capture an installation or change a selector.
"""

from muli.platforms.managed_launcher import RepairStatus, inspect_repair

__all__ = ["RepairStatus", "inspect_repair"]
