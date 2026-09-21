"""Immutable per-call role bindings over one cumulative LocalCodex ledger/cap."""
from __future__ import annotations


class RoleHost:
    def __init__(self, shared, role: str, profile: dict):
        self.shared, self.role = shared, role
        self.model, self.effort = profile["model"], profile["reasoning_effort"]
        self.service_tier = profile.get("service_tier", getattr(shared, "service_tier", "fast"))
        self.phase = role

    @property
    def calls(self):
        return self.shared.calls

    @property
    def rejections(self):
        return self.shared.rejections

    def complete(self, instructions, state, schema):
        return self.shared.complete(instructions, state, schema, model=self.model,
                                    effort=self.effort, phase=self.phase, role=self.role, service_tier=self.service_tier)

    async def acomplete(self, instructions, state, schema):
        return await self.shared.acomplete(instructions, state, schema, model=self.model,
                                          effort=self.effort, phase=self.phase, role=self.role, service_tier=self.service_tier)
