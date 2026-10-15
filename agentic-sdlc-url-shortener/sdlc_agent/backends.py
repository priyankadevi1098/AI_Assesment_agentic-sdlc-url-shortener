"""Backend selection contract shared by CLI coding-assistant and API adapters.

Keeps selection, destination-approval and mid-run-fallback rules in one small,
framework-free place so every adapter (``cli_backends.py``, ``llm.py``) follows
the same contract instead of re-deriving it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence


class BackendError(RuntimeError):
    """Base error for backend selection and invocation failures."""


class NoCompatibleBackendError(BackendError):
    """Raised when no backend (CLI or API) is usable for the requested selection."""


class BackendSelectionAmbiguousError(BackendError):
    """Raised when more than one backend is usable and no preference was configured."""

    def __init__(self, available: Sequence[str]):
        super().__init__(
            "multiple compatible backends are available "
            f"({', '.join(available)}); configure an explicit preference"
        )
        self.available = tuple(available)


class DestinationApprovalRequiredError(BackendError):
    """Raised when requirement or source content would be sent before destination approval."""

    def __init__(self, backend_name: str):
        super().__init__(f"destination approval required before sending content to {backend_name!r}")
        self.backend_name = backend_name


class MidRunFallbackProhibitedError(BackendError):
    """Raised when a run tries to switch backend after work has started without re-approval."""

    def __init__(self, locked_backend: str, requested_backend: str):
        super().__init__(
            f"run is locked to backend {locked_backend!r}; switching to "
            f"{requested_backend!r} requires explicit re-approval, not a silent fallback"
        )
        self.locked_backend = locked_backend
        self.requested_backend = requested_backend


@dataclass(frozen=True)
class CliCapability:
    """Preflight result for one CLI coding-assistant adapter."""

    name: str
    executable: Optional[str]
    version: Optional[str]
    supports_inference_only: bool
    supported: bool
    reason: Optional[str]


Approver = Callable[[str], bool]


class BackendSelector:
    """Chooses a backend from injected candidate CLI adapters plus the API fallback.

    Candidates are injected rather than constructed here so tests can supply fakes
    without touching a real subprocess or network call.
    """

    def __init__(self, cli_adapters: Sequence, api_backend_name: str = "api"):
        self._cli_adapters = list(cli_adapters)
        self._api_backend_name = api_backend_name

    def discover(self) -> List[CliCapability]:
        """Run non-mutating preflight capability checks on every candidate CLI adapter."""
        return [adapter.preflight() for adapter in self._cli_adapters]

    def select(self, explicit: str, preference: Optional[str] = None,
               api_available: bool = False) -> str:
        """Return the name of the selected backend, applying the auto-selection rules.

        Args:
            explicit: ``"auto"``, an adapter name, or the API backend name.
            preference: required to break ties when more than one CLI is usable
                under ``"auto"``.
            api_available: whether API credentials are configured.

        Raises:
            NoCompatibleBackendError: the explicit choice, or auto-selection, found
                nothing usable.
            BackendSelectionAmbiguousError: multiple CLIs are usable and no
                preference was given.
        """
        if explicit != "auto":
            return self._select_explicit(explicit, api_available)
        return self._select_auto(preference, api_available)

    def _select_explicit(self, explicit: str, api_available: bool) -> str:
        if explicit == self._api_backend_name:
            if not api_available:
                raise NoCompatibleBackendError(
                    f"{self._api_backend_name} backend requested but no API credentials are configured"
                )
            return explicit

        capability = next((c for c in self.discover() if c.name == explicit), None)
        if capability is None or not capability.supported:
            reason = capability.reason if capability else f"{explicit} is not a known backend"
            raise NoCompatibleBackendError(f"{explicit} is not usable: {reason}")
        return explicit

    def _select_auto(self, preference: Optional[str], api_available: bool) -> str:
        usable = [c.name for c in self.discover() if c.supported]

        if len(usable) > 1:
            if preference is None:
                raise BackendSelectionAmbiguousError(usable)
            if preference not in usable:
                raise NoCompatibleBackendError(
                    f"preferred backend {preference!r} is not among usable backends {usable}"
                )
            return preference

        if len(usable) == 1:
            return usable[0]

        if api_available:
            return self._api_backend_name

        raise NoCompatibleBackendError("no compatible CLI assistant and no API credentials configured")


def require_destination_approval(backend_name: str, approver: Approver) -> None:
    """Raise unless the approver explicitly approves sending content to ``backend_name``.

    Raises:
        DestinationApprovalRequiredError: the approver declined or was not asked.
    """
    if not approver(backend_name):
        raise DestinationApprovalRequiredError(backend_name)


class BackendLock:
    """Tracks the backend a run committed to, rejecting silent mid-run fallback."""

    def __init__(self, locked_backend: str):
        self.locked_backend = locked_backend

    def ensure(self, requested_backend: str, *, reapproved: bool = False) -> None:
        """Raise unless the requested backend matches the lock or was explicitly re-approved.

        Raises:
            MidRunFallbackProhibitedError: a different backend was requested without
                ``reapproved=True``.
        """
        if requested_backend != self.locked_backend and not reapproved:
            raise MidRunFallbackProhibitedError(self.locked_backend, requested_backend)


def ensure_tools_disabled(invocation_args: Sequence[str], required_disable_markers: Sequence[str]) -> None:
    """Raise unless every required inference-only marker is present in the invocation.

    Shell, file-write and network tool access must be disabled before any CLI
    invocation. This checks the literal argument list an adapter is about to run,
    before a subprocess is ever started.

    Raises:
        PermissionError: a required permission-restricting marker is missing.
    """
    missing = [marker for marker in required_disable_markers if marker not in invocation_args]
    if missing:
        raise PermissionError(
            f"refusing to invoke CLI without required permission-restricting flags: {missing}"
        )
