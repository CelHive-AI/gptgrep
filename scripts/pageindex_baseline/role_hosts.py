"""Role views over one existing LocalCodex ledger, lock and global invocation cap."""
from __future__ import annotations


class RoleHost:
    def __init__(self, shared, role: str, profile: dict):
        self.shared, self.role = shared, role
        self.model, self.effort = profile["model"], profile["reasoning_effort"]
        self.phase = role

    @property
    def calls(self):
        return self.shared.calls

    @property
    def rejections(self):
        return self.shared.rejections

    def complete(self, instructions, state, schema):
        # Delegate unchanged payload bytes through the capability-qualified bridge.
        # One shared lock prevents concurrent role changes or independent budgets.
        with self.shared._lock:
            previous = self.shared.model, self.shared.effort, self.shared.phase
            self.shared.model, self.shared.effort, self.shared.phase = self.model, self.effort, self.phase
            try:
                return self.shared.complete(instructions, state, schema)
            finally:
                self.shared.model, self.shared.effort, self.shared.phase = previous
