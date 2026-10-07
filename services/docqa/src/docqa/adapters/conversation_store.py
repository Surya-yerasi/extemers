"""Conversations as JSON objects in a BlobStore: S3 when deployed (the encrypted documents
bucket), local files in local mode. One object per conversation plus a per-user index, so
the sidebar needs one read, not one per conversation.

    conversations/<owner hash>/index.json
    conversations/<owner hash>/<conversation id>.json
"""

import hashlib
from collections.abc import Sequence

from pydantic import TypeAdapter

from docqa.domain.conversation import ConversationSummary, is_conversation_id
from docqa.pipelines.chat import Conversation
from docqa.ports import BlobStore

PREFIX = "conversations/"
_INDEX = TypeAdapter(list[ConversationSummary])


def _owner_key(owner: str) -> str:
    # Hashed so keys never contain user-controlled characters.
    return hashlib.sha256(owner.encode()).hexdigest()[:32]


class BlobConversationStore:
    def __init__(self, blobs: BlobStore) -> None:
        self._blobs = blobs

    def _key(self, owner: str, conversation_id: str) -> str:
        if not is_conversation_id(conversation_id):
            raise ValueError("invalid conversation id")
        return f"{PREFIX}{_owner_key(owner)}/{conversation_id}.json"

    def _index_key(self, owner: str) -> str:
        return f"{PREFIX}{_owner_key(owner)}/index.json"

    def list(self, owner: str) -> list[ConversationSummary]:
        key = self._index_key(owner)
        if not self._blobs.exists(key):
            return []
        return _INDEX.validate_json(self._blobs.get(key))

    def get(self, owner: str, conversation_id: str) -> Conversation | None:
        if not is_conversation_id(conversation_id):
            return None
        key = self._key(owner, conversation_id)
        if not self._blobs.exists(key):
            return None
        return Conversation.model_validate_json(self._blobs.get(key))

    def save(self, conversation: Conversation) -> None:
        owner = conversation.owner
        self._blobs.put(
            self._key(owner, conversation.conversation_id),
            conversation.model_dump_json().encode(),
            "application/json",
        )
        others = [s for s in self.list(owner) if s.conversation_id != conversation.conversation_id]
        self._write_index(owner, [conversation.summary(), *others])

    def delete(self, owner: str, conversation_id: str) -> None:
        self._blobs.delete(self._key(owner, conversation_id))
        remaining = [s for s in self.list(owner) if s.conversation_id != conversation_id]
        self._write_index(owner, remaining)

    def _write_index(self, owner: str, summaries: Sequence[ConversationSummary]) -> None:
        newest_first = sorted(summaries, key=lambda s: s.updated_at, reverse=True)
        self._blobs.put(self._index_key(owner), _INDEX.dump_json(newest_first), "application/json")
