"""Immutable per-call role bindings over one cumulative LocalCodex ledger/cap."""
from __future__ import annotations


class RoleHost:
    def __init__(self, shared, role: str, profile: dict, *, phase=None, on_reserved=None):
        self.shared, self.role = shared, role
        self.model, self.effort = profile["model"], profile["reasoning_effort"]
        self.service_tier = profile.get("service_tier", getattr(shared, "service_tier", "fast"))
        self.phase = role if phase is None else phase
        self.on_reserved = on_reserved

    @property
    def calls(self):
        return self.shared.calls

    @property
    def rejections(self):
        return self.shared.rejections

    def complete(self, instructions, state, schema, *, on_reserved=None):
        on_reserved = self.on_reserved if on_reserved is None else on_reserved
        return self.shared.complete(instructions, state, schema, model=self.model,
                                    effort=self.effort, phase=self.phase, role=self.role, service_tier=self.service_tier,
                                    **({"on_reserved": on_reserved} if on_reserved is not None else {}))

    async def acomplete(self, instructions, state, schema):
        return await self.shared.acomplete(instructions, state, schema, model=self.model,
                                          effort=self.effort, phase=self.phase, role=self.role, service_tier=self.service_tier,
                                          **({"on_reserved": self.on_reserved} if self.on_reserved is not None else {}))
