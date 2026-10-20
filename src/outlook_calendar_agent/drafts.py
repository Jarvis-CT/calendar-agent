"""Saved drafts: proposals that have been previewed but not yet confirmed. Stored mode 600."""

from __future__ import annotations

import re
import secrets
from datetime import UTC, datetime
from pathlib import Path

from .errors import AgentError
from .models import Draft, DraftPayload
from .storage import ensure_private_dir, write_private_text

_DRAFT_ID_RE = re.compile(r"^d-[0-9a-f]{6}$")
_ID_SEPARATOR_RE = re.compile(r"[\s,;]+")


def draft_ids(text: str) -> list[str]:
    """The draft ids named in ``text``: one, or several separated by commas or spaces.

    Several ids make one batch, so a user who asked for five events in one message gives one
    approval instead of five. Order is kept and repeats are dropped; any bad id fails the lot."""
    ids: list[str] = []
    for token in _ID_SEPARATOR_RE.split(text.strip()):
        if token and not _DRAFT_ID_RE.match(token):
            raise AgentError(f"Invalid draft ID {token!r}.", hint="Draft IDs look like d-1a2b3c.")
        if token and token not in ids:
            ids.append(token)
    if not ids:
        raise AgentError(f"Invalid draft ID {text!r}.", hint="Draft IDs look like d-1a2b3c.")
    return ids


class DraftStore:
    def __init__(self, directory: Path) -> None:
        self._dir = directory

    def _path(self, draft_id: str) -> Path:
        if not _DRAFT_ID_RE.match(draft_id):
            raise AgentError(
                f"Invalid draft ID {draft_id!r}.", hint="Draft IDs look like d-1a2b3c."
            )
        return self._dir / f"{draft_id}.json"

    def save(self, payload: DraftPayload) -> Draft:
        ensure_private_dir(self._dir)
        draft_id = f"d-{secrets.token_hex(3)}"
        while self._path(draft_id).exists():
            draft_id = f"d-{secrets.token_hex(3)}"
        draft = Draft(id=draft_id, created_at=datetime.now(UTC), payload=payload)
        write_private_text(self._path(draft_id), draft.model_dump_json(indent=2))
        return draft

    def load(self, draft_id: str) -> Draft:
        path = self._path(draft_id)
        if not path.exists():
            raise AgentError(
                f"Draft {draft_id} not found.", hint="Run 'drafts' to list saved drafts."
            )
        return Draft.model_validate_json(path.read_text(encoding="utf-8"))

    def discard(self, draft_id: str) -> None:
        self._path(draft_id).unlink(missing_ok=True)

    def list(self) -> list[Draft]:
        if not self._dir.exists():
            return []
        drafts = [
            Draft.model_validate_json(p.read_text(encoding="utf-8"))
            for p in sorted(self._dir.glob("d-*.json"))
        ]
        return sorted(drafts, key=lambda d: d.created_at)
