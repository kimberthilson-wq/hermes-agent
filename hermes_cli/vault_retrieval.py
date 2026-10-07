"""Explicit read-only vault retrieval boundary.

This is the **single public entry point** for project-aware vault retrieval.
It wraps the bounded ``read_project_vault_hub`` primitive with a caller
contract:

* Accepts an exact ``project_id`` (Registry identity, not a filesystem path).
* Rejects inputs that look like filesystem paths (``/``, ``\\``, ``..``, ``~``).
* Delegates to ``read_project_vault_hub`` unchanged.
* Propagates all fail-closed exceptions.
* Returns the ``VaultHubResult`` with provenance intact.

It performs **none** of:
* Vault traversal or search
* Project discovery
* Registry synchronisation
* Automatic context injection
* Memory / skill / config / secret writes
* Proposal persistence
* Approval handling
* Caching / indexing / background retrieval
* CLI commands
* Agent tools
* Database migrations

This is the narrowest possible integration: one validation gate + one
delegation call. Future project-aware retrieval calls ``retrieve_project_vault``
intentionally and explicitly.
"""

from __future__ import annotations

import re
from typing import Optional

from hermes_cli.vault_hub import (
    VaultHubError,
    VaultHubProjectNotFound,
    VaultHubReferenceMissing,
    VaultHubOutsideRoot,
    VaultHubTargetInvalid,
    VaultHubResult,
    DEFAULT_MAX_CHARS,
    read_project_vault_hub,
)

__all__ = [
    "retrieve_project_vault",
    "VaultHubInvalidProjectId",
    "VaultHubError",
    "VaultHubProjectNotFound",
    "VaultHubReferenceMissing",
    "VaultHubOutsideRoot",
    "VaultHubTargetInvalid",
    "VaultHubResult",
]


class VaultHubInvalidProjectId(VaultHubError):
    """The caller supplied something that is not a valid Registry project_id.

    Raised when the input contains filesystem-path characters (``/``, ``\\``,
    ``..``, ``~``, leading/trailing whitespace, or is empty) or does not
    match the expected ``[a-z0-9_]+`` pattern used by the Registry.
    """


#: Registry project_ids are lowercase alphanumeric + underscore (e.g.
#: ``trion_application``, ``rc_pt_experiment``, ``hermes_memory_pilot``).
_PROJECT_ID_RE = re.compile(r"^[a-z0-9_]+$")

#: Characters / patterns that indicate a filesystem path rather than an id.
_PATH_INDICATORS = ("/", "\\", "..", "~", " ", "\t", "\n", "\r", "\0")


def _looks_like_path(value: str) -> bool:
    """Return True if ``value`` has any filesystem-path indicator."""
    if not value:
        return True
    for indicator in _PATH_INDICATORS:
        if indicator in value:
            return True
    return False


def retrieve_project_vault(
    project_id: str,
    *,
    max_chars: Optional[int] = None,
) -> VaultHubResult:
    """Read the ``vault_hub`` file bound to ``project_id`` (explicit caller).

    This is the **explicit, intentional** boundary for project-aware vault
    retrieval. Callers that need vault content for a known project call this
    function — they do NOT reach into ``vault_hub.py`` directly, and they do
    NOT pass a filesystem path.

    Args:
        project_id: exact Registry ``project_id`` (e.g. ``"trion_application"``).
            Must NOT be a filesystem path, URL, or arbitrary string.
        max_chars: optional character bound (defaults to 20 000, hard-capped).

    Returns:
        ``VaultHubResult`` with bounded content + provenance.

    Raises:
        VaultHubInvalidProjectId: input is a path / malformed / empty.
        VaultHubProjectNotFound: project_id not in Registry or ambiguous.
        VaultHubReferenceMissing: vault_hub absent / empty / UNKNOWN.
        VaultHubOutsideRoot: target escapes knowledge-vault root.
        VaultHubTargetInvalid: target missing or not a regular file.

    Side effects: **none.** Pure read.
    """
    # --- caller-side validation (the integration gate) --------------------
    if not isinstance(project_id, str):
        raise VaultHubInvalidProjectId(
            f"project_id must be str, got {type(project_id).__name__}"
        )
    stripped = project_id.strip()
    if not stripped:
        raise VaultHubInvalidProjectId("project_id must not be empty")
    if _looks_like_path(stripped):
        raise VaultHubInvalidProjectId(
            f"project_id {project_id!r} looks like a filesystem path; "
            "pass the Registry project_id, not a path"
        )
    if not _PROJECT_ID_RE.match(stripped):
        raise VaultHubInvalidProjectId(
            f"project_id {project_id!r} does not match expected pattern "
            "[a-z0-9_]+ (Registry identity)"
        )

    # --- delegate to the bounded primitive (unchanged) --------------------
    if max_chars is None:
        return read_project_vault_hub(stripped)
    return read_project_vault_hub(stripped, max_chars=max_chars)
