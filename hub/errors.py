"""Errors raised by the hub. Each one names what went wrong in plain words."""


class HubError(Exception):
    """Base class, so callers can catch every hub refusal in one place."""


class ClockError(HubError):
    """The simulated clock was asked to do something it must not (go backwards, take a naive time)."""


class StaleVersionError(HubError):
    """Optimistic concurrency: the writer expected version n, but the current version is not n.

    Raised instead of overwriting, so two agents can never both build on the same version.
    """

    def __init__(self, key: str, expected: int, actual: int | None):
        self.key, self.expected, self.actual = key, expected, actual
        current = "none (no open version)" if actual is None else str(actual)
        super().__init__(f"{key}: expected version {expected}, current is {current}")


class IllegalTransitionError(HubError):
    """A commitment status change that SPEC sections 7 and 17.8 do not allow."""

    def __init__(self, unit_id: str, from_status: str, to_status: str):
        self.unit_id, self.from_status, self.to_status = unit_id, from_status, to_status
        super().__init__(f"{unit_id}: transition {from_status} -> {to_status} is not allowed")
