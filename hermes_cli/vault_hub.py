"""Read-only ``vault_hub`` retrieval — the Step 30 approved contract, nothing more.

This module implements the **minimal** bounded retrieval described in Step 30:

    exact project_id  ->  HERMES_PROJECT_REGISTRY.md ``vault_hub`` reference
                      ->  containment check inside the knowledge-vault root
                      ->  single-file, character-bounded read
                      ->  provenance

It is **read-only** and deliberately NOT a general Registry subsystem:

* It parses the Registry as a *data source* (the ``<!-- registry-entry -->`` blocks
  for the exact requested ``project_id``) and nothing else.
* It performs exact ``project_id`` resolution only — no fuzzy / prefix / cwd /
  direct-path / note-body / directory-name inference.
* It reads exactly one file: the explicit ``vault_hub`` target.
* It bounds every read by a character limit; unbounded reads are impossible.
* It returns provenance alongside the content.
* It performs no writes of any kind: no Registry, Vault, DB, memory, skill,
  config, secret, or proposal persistence. No LLM/context auto-injection.

The vault-root containment check reuses ``hermes_cli.projects_db.is_within``
against the **knowledge-vault root** — NOT against ``canonical_root``. The two
are separate namespaces (verified in Step 30: vault support notes are NOT under
a project's ``canonical_root`` in the general case).
"""

from __future__ import annotations

import os
import stat
from inspect import unwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from hermes_cli.projects_db import is_within

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_MAX_CHARS = 20_000
_MAX_CHARS_HARD_LIMIT = 500_000

#: The knowledge-vault root is a fixed, well-known location under HERMES_HOME.
_VAULT_DIRNAME = "knowledge-vault"

#: Fields surfaced in provenance (all drawn from the single Registry entry).
_PROVENANCE_FIELDS = (
    "project_id",
    "vault_hub",
    "canonical_root",
    "ownership_class",
    "default_access_mode",
    "source",
)


class VaultHubError(Exception):
    """Base for every fail-closed condition in ``read_project_vault_hub``."""


class VaultHubProjectNotFound(VaultHubError):
    """The Registry has no entry for the exact ``project_id``."""


class VaultHubReferenceMissing(VaultHubError):
    """The entry has no usable ``vault_hub`` (absent, empty, or ``UNKNOWN``)."""


class VaultHubOutsideRoot(VaultHubError):
    """The resolved ``vault_hub`` target is not inside the knowledge-vault root."""


class VaultHubTargetInvalid(VaultHubError):
    """The resolved target does not exist or is not a regular file."""


# ---------------------------------------------------------------------------
# Registry read (minimal, exact-id, read-only)
# ---------------------------------------------------------------------------


def _vault_root(hermes_home: Optional[Path] = None) -> Path:
    """The knowledge-vault root under ``HERMES_HOME`` (default: live location).

    Overridable in tests by passing an explicit ``hermes_home`` fixture root.
    """
    if hermes_home is not None:
        return Path(hermes_home) / _VAULT_DIRNAME
    from hermes_constants import get_hermes_home

    return get_hermes_home() / _VAULT_DIRNAME


def _entry_blocks(registry_text: str):
    """Yield the raw text of each ``<!-- registry-entry --> ... <!-- /registry-entry -->`` block.

    Minimal marker-scoped parser; it does not interpret the file beyond the
    entry delimiters and the single-line ``key: value`` fields inside them.
    """
    open_tag = "<!-- registry-entry -->"
    close_tag = "<!-- /registry-entry -->"
    pos = 0
    while True:
        start = registry_text.find(open_tag, pos)
        if start == -1:
            return
        body_start = start + len(open_tag)
        end = registry_text.find(close_tag, body_start)
        if end == -1:
            return  # unterminated block — stop, do not fabricate a value
        yield registry_text[body_start:end]
        pos = end + len(close_tag)


def _parse_fields(block: str) -> dict:
    """Parse single-line ``key: value`` pairs from one entry block.

    Values are taken verbatim (stripped). Multi-line / list values are not
    interpreted; only the flat identity fields required by the contract are
    read. Unknown lines are ignored.
    """
    fields: dict = {}
    for line in block.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        # Only keep the identity/routing fields the contract needs.
        if key in (
            "project_id",
            "project_name",
            "vault_hub",
            "canonical_root",
            "canonical_source",
            "canonical_source_type",
            "ownership_class",
            "default_access_mode",
            "owner",
            "status_scope",
            "status",
        ):
            if key in fields:
                raise VaultHubProjectNotFound(f"duplicate Registry field: {key}")
            fields[key] = value
    return fields


def _registry_lookup(project_id: str, registry_path: Path) -> dict:
    """Return the parsed fields for the exact ``project_id``, or raise.

    Exact match only: a single entry whose ``project_id`` equals ``project_id``.
    Duplicate ids are rejected (fail-closed) rather than silently choosing one.
    """
    if not project_id or not project_id.strip():
        raise VaultHubProjectNotFound("project_id is empty")
    try:
        text = registry_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise VaultHubProjectNotFound(f"Registry unreadable: {exc}") from exc

    matches = []
    for block in _entry_blocks(text):
        fields = _parse_fields(block)
        if fields.get("project_id") == project_id:
            matches.append(fields)
    if not matches:
        raise VaultHubProjectNotFound(f"project_id not in Registry: {project_id!r}")
    if len(matches) > 1:
        raise VaultHubProjectNotFound(
            f"project_id {project_id!r} appears {len(matches)} times; refusing ambiguous match"
        )
    return matches[0]


def resolve_registry_entry(project_id: str, *, registry_path: Optional[Path] = None) -> dict:
    """Public, exact-id Registry identity lookup (read-only) — the single Registry
    entry point for the project resolver.

    This is the REUSE surface for ``hermes_cli.projects_db.resolve_project``:
    it parses the Registry exactly once, for the exact ``project_id`` (no fuzzy /
    prefix / cwd / path inference), and returns the parsed identity/policy fields
    (``project_id``, ``project_name``, ``ownership_class``,
    ``canonical_source_type``, ``default_access_mode``, ``canonical_root``,
    ``status`` / ``status_scope``, ``owner``) verbatim. It does NOT read the
    ``vault_hub`` file and performs no writes.

    Fail-closed: a missing / empty / duplicate ``project_id`` raises
    :class:`VaultHubProjectNotFound`; an unreadable Registry raises the same.
    """
    if registry_path is None:
        registry_path = _vault_root() / "HERMES_PROJECT_REGISTRY.md"
    return _registry_lookup(project_id, registry_path)


# ---------------------------------------------------------------------------
# Public result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VaultHubResult:
    """Bounded single-file vault read plus its provenance.

    ``provenance`` carries, verbatim from the single Registry entry:
    ``project_id``, ``vault_hub``, ``canonical_root``, ``ownership_class``,
    ``default_access_mode`` and ``source`` (the Registry file name). A field
    that is absent or ``UNKNOWN`` in the Registry is preserved as ``"UNKNOWN"``
    (or ``""``) rather than inferred — the Registry fail-closed policy.
    """

    project_id: str
    content: str
    truncated: bool
    provenance: dict


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def read_project_vault_hub(
    project_id: str,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    registry_path: Optional[Path] = None,
    hermes_home: Optional[Path] = None,
) -> VaultHubResult:
    """Read the exact ``vault_hub`` file bound to ``project_id`` (read-only).

    Contract (Step 30), fail-closed at every step:

    1. Read ``HERMES_PROJECT_REGISTRY.md`` as a read-only data source.
    2. Resolve the **exact** ``project_id`` (no fuzzy / prefix / cwd / path /
       note-body / directory-name inference).
    3. Read only that entry's explicit ``vault_hub`` reference.
    4. Fail closed if: ``project_id`` missing/empty, entry missing, ``vault_hub``
       missing / empty / ``UNKNOWN``, the target cannot be resolved safely
       inside the knowledge-vault root, the target does not exist, or the
       target is not a regular file.
    5. Containment is checked against the **knowledge-vault root** (NOT against
       a project's ``canonical_root`` — separate namespaces).
    6. Read exactly that one file, bounded by ``max_chars``.
    7. Return the content together with provenance.

    No writes of any kind; no auto-injection; no Registry/Vault/DB/memory/skill
    mutation.

    Args:
        project_id: exact Registry ``project_id``.
        max_chars: maximum characters returned (hard-capped). Default 20_000.
        registry_path: explicit Registry path (tests). Default: the live one.
        hermes_home: explicit HERMES_HOME root for locating the vault (tests).

    Raises:
        VaultHubProjectNotFound, VaultHubReferenceMissing, VaultHubOutsideRoot,
        VaultHubTargetInvalid — all fail-closed.
    """
    if registry_path is None:
        registry_path = _vault_root(hermes_home) / "HERMES_PROJECT_REGISTRY.md"

    entry = _registry_lookup(project_id, registry_path)

    vault_root = _vault_root(hermes_home)

    # --- vault_hub: explicit reference only -------------------------------
    raw_hub = entry.get("vault_hub")
    if raw_hub is None or raw_hub == "" or raw_hub.strip() == "":
        raise VaultHubReferenceMissing("vault_hub is missing or empty")
    raw_hub = raw_hub.strip()
    if raw_hub == "UNKNOWN":
        raise VaultHubReferenceMissing("vault_hub is UNKNOWN (fail-closed)")

    # The Registry ``vault_hub`` value is a vault-relative or absolute path.
    # Resolve it as a path relative to the vault root when not absolute.
    candidate = Path(raw_hub)
    if not candidate.is_absolute():
        candidate = vault_root / raw_hub
    target = candidate

    # --- containment: inside the knowledge-vault root ----------------------
    # Reuse the symlink-safe, separator-boundary helper — but against the
    # VAULT ROOT, not canonical_root (separate namespaces; Step 30).
    if not is_within(str(vault_root), str(target)):
        raise VaultHubOutsideRoot(f"vault_hub target escapes knowledge-vault root: {target}")

    # --- target must exist and be a regular file --------------------------
    if not target.exists():
        raise VaultHubTargetInvalid(f"vault_hub target does not exist: {target}")
    if not target.is_file():
        raise VaultHubTargetInvalid(f"vault_hub target is not a regular file: {target}")

    # --- bounded single-file read -----------------------------------------
    limit = _coerce_max_chars(max_chars)
    text = _bounded_read(target, limit, vault_root=vault_root)
    truncated = len(text) >= limit  # conservative: we read no more than the limit

    # --- provenance (verbatim from the entry; never inferred) -------------
    provenance = {
        "project_id": entry.get("project_id", project_id),
        "vault_hub": raw_hub,
        "canonical_root": _or_unknown(entry.get("canonical_root")),
        "ownership_class": _or_unknown(entry.get("ownership_class")),
        "default_access_mode": _or_unknown(entry.get("default_access_mode")),
        "source": "HERMES_PROJECT_REGISTRY.md",
    }

    return VaultHubResult(
        project_id=project_id,
        content=text,
        truncated=truncated,
        provenance=provenance,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _or_unknown(value: Optional[str]) -> str:
    """Preserve the Registry value verbatim; missing/empty -> ``"UNKNOWN"``."""
    if value is None or value.strip() == "":
        return "UNKNOWN"
    return value.strip()


def _coerce_max_chars(max_chars: int) -> int:
    try:
        n = int(max_chars)
    except (TypeError, ValueError):
        n = DEFAULT_MAX_CHARS
    if n <= 0:
        n = DEFAULT_MAX_CHARS
    return min(n, _MAX_CHARS_HARD_LIMIT)


def _bounded_read(path: Path, limit: int, *, vault_root: Path) -> str:
    """Read a regular file through no-follow descriptors anchored to the vault.

    Resolve existing in-vault symlinks before opening, then open every resolved
    component relative to the pinned root descriptor. A replaced component is
    never followed; file type is checked on the descriptor actually read.
    Platforms without these safe primitives fail closed.
    """
    if (
        not all(hasattr(os, flag) for flag in ("O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK"))
        or unwrap(os.open) not in os.supports_dir_fd
    ):
        raise VaultHubTargetInvalid("safe descriptor-based vault reads unavailable")

    try:
        root = vault_root.resolve(strict=True)
        target = path.resolve(strict=True)
        try:
            relative = target.relative_to(root)
        except ValueError as exc:
            raise VaultHubOutsideRoot("vault_hub target escapes knowledge-vault root") from exc
        if not relative.parts:
            raise VaultHubTargetInvalid("vault_hub target is not a regular file")

        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        directory_fd = os.open(root, directory_flags)
        try:
            for component in relative.parts[:-1]:
                child_fd = os.open(component, directory_flags, dir_fd=directory_fd)
                os.close(directory_fd)
                directory_fd = child_fd
            file_fd = os.open(
                relative.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=directory_fd,
            )
        finally:
            os.close(directory_fd)

        try:
            if not stat.S_ISREG(os.fstat(file_fd).st_mode):
                raise VaultHubTargetInvalid("vault_hub target is not a regular file")
            with os.fdopen(file_fd, "r", encoding="utf-8", errors="replace") as fh:
                file_fd = None  # fdopen owns and closes the descriptor.
                return fh.read(limit)
        finally:
            if file_fd is not None:
                os.close(file_fd)
    except (OSError, UnicodeDecodeError) as exc:
        raise VaultHubTargetInvalid(f"vault_hub target unreadable: {exc}") from exc
