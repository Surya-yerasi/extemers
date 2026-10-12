"""Builds the real services from settings (composition root).

The provider setting picks the adapters: Bedrock + S3 when deployed, Ollama + local files in
local mode. The pipelines themselves do not know which one they run on.
"""

from typing import Any

from docqa.adapters.bedrock import (
    BedrockGenerator,
    BedrockMetadataExtractor,
    BedrockVisionTranscriber,
    CohereReranker,
    TitanEmbedder,
    bedrock_runtime,
)
from docqa.adapters.conversation_store import BlobConversationStore
from docqa.adapters.eval_runs import EvalRunStore
from docqa.adapters.lancedb_index import LanceChunkIndex, LanceChunkSearcher, LanceDocumentCatalog
from docqa.adapters.local_blobs import LocalBlobStore
from docqa.adapters.ollama import (
    OllamaClient,
    OllamaEmbedder,
    OllamaGenerator,
    OllamaMetadataExtractor,
    OllamaVisionTranscriber,
    PassthroughReranker,
)
from docqa.adapters.s3_blobs import S3BlobStore
from docqa.config import IngestSettings, Provider, QASettings
from docqa.domain.agent import AgentConfig
from docqa.domain.chunking import ChunkingConfig
from docqa.pipelines.chat import ChatService
from docqa.pipelines.documents import DocumentService
from docqa.pipelines.ingest import IngestService
from docqa.pipelines.qa import QAService


def build_ingest_service(settings: IngestSettings) -> IngestService:
    index = LanceChunkIndex(
        settings.resolved_lancedb_uri, settings.index_table, settings.embedding_dimensions
    )
    chunking = ChunkingConfig(settings.chunk_max_tokens, settings.chunk_overlap_tokens)
    if settings.provider is Provider.LOCAL:
        client = OllamaClient(settings.ollama_url)
        return IngestService(
            blobs=LocalBlobStore(settings.local_data_dir),
            vision=OllamaVisionTranscriber(client, settings.vision_model_id),
            metadata=OllamaMetadataExtractor(client, settings.metadata_model_id),
            embedder=OllamaEmbedder(
                client, settings.embedding_model_id, settings.embedding_dimensions
            ),
            index=index,
            chunking=chunking,
            index_table=settings.index_table,
        )
    if not settings.docs_bucket:  # IngestSettings already enforces this; narrows the type
        raise ValueError("DOCQA_DOCS_BUCKET is required with the bedrock provider")
    runtime = bedrock_runtime()
    return IngestService(
        blobs=S3BlobStore(settings.docs_bucket),
        vision=BedrockVisionTranscriber(settings.vision_model_id, runtime),
        metadata=BedrockMetadataExtractor(settings.metadata_model_id, runtime),
        embedder=TitanEmbedder(settings.embedding_model_id, settings.embedding_dimensions, runtime),
        index=index,
        chunking=chunking,
        index_table=settings.index_table,
    )


def build_qa_service(settings: QASettings, docs_bucket: str | None) -> QAService:
    searcher = LanceChunkSearcher(settings.lancedb_uri_for(docs_bucket), settings.index_table)
    limits: dict[str, Any] = {
        "candidates": settings.candidates,
        "top_k": settings.top_k,
        "max_answer_tokens": settings.max_answer_tokens,
        "agent_config": AgentConfig(
            max_retrievals=settings.agent_max_retrievals,
            verify=settings.agent_verify,
            top_k=settings.top_k,
        ),
    }
    if settings.provider is Provider.LOCAL:
        client = OllamaClient(settings.ollama_url)
        embedder = OllamaEmbedder(
            client, settings.embedding_model_id, settings.embedding_dimensions
        )
        return QAService(
            searcher=searcher,
            embedder=embedder,
            embedding_model_id=embedder.model_id,
            generator=OllamaGenerator(client, settings.generation_model_id),
            reranker=PassthroughReranker(),
            **limits,
        )
    runtime = bedrock_runtime()
    return QAService(
        searcher=searcher,
        embedder=TitanEmbedder(settings.embedding_model_id, settings.embedding_dimensions, runtime),
        embedding_model_id=settings.embedding_model_id,
        generator=BedrockGenerator(settings.generation_model_id, runtime),
        reranker=CohereReranker(settings.rerank_model_id, runtime),
        **limits,
    )


def build_eval_run_store(settings: QASettings, docs_bucket: str | None) -> EvalRunStore:
    """Eval runs sit under evals/ next to everything else (.data/evals/ in local mode)."""
    if settings.provider is Provider.LOCAL:
        return EvalRunStore(LocalBlobStore(settings.local_data_dir))
    if not docs_bucket:
        raise ValueError("a documents bucket is required with the bedrock provider")
    return EvalRunStore(S3BlobStore(docs_bucket))


def build_blob_store(settings: QASettings, docs_bucket: str | None) -> LocalBlobStore | S3BlobStore:
    """Local files in local mode, the encrypted documents bucket otherwise."""
    if settings.provider is Provider.LOCAL:
        return LocalBlobStore(settings.local_data_dir)
    if not docs_bucket:
        raise ValueError("a documents bucket is required with the bedrock provider")
    return S3BlobStore(docs_bucket)


def build_chat_service(settings: QASettings, docs_bucket: str | None) -> ChatService:
    """Conversations live next to the documents: local files, or the encrypted bucket."""
    blobs = build_blob_store(settings, docs_bucket)
    return ChatService(build_qa_service(settings, docs_bucket), BlobConversationStore(blobs))


def build_document_service(settings: QASettings, docs_bucket: str | None) -> DocumentService:
    """Lists raw/ and reads ingest-status/ and the index; never reads the documents."""
    catalog = LanceDocumentCatalog(settings.lancedb_uri_for(docs_bucket), settings.index_table)
    if settings.provider is Provider.LOCAL:
        data = settings.local_data_dir
        where = data if data.is_absolute() else f"services/docqa/{data}"  # make runs from the root
        return DocumentService(
            LocalBlobStore(settings.local_data_dir),
            catalog,
            settings.index_table,
            source=f"{where}/raw/",
            ingest_hint="Run make docqa-local-ingest; this page follows its progress.",
        )
    if not docs_bucket:
        raise ValueError("a documents bucket is required with the bedrock provider")
    return DocumentService(
        S3BlobStore(docs_bucket),
        catalog,
        settings.index_table,
        source=f"s3://{docs_bucket}/raw/",
        ingest_hint="Ingestion starts automatically on upload; this page follows it.",
    )
