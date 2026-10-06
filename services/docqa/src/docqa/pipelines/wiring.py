"""Builds the real services from settings (composition root)."""

from docqa.adapters.bedrock import (
    BedrockGenerator,
    BedrockMetadataExtractor,
    BedrockVisionTranscriber,
    CohereReranker,
    TitanEmbedder,
    bedrock_runtime,
)
from docqa.adapters.lancedb_index import LanceChunkIndex, LanceChunkSearcher
from docqa.adapters.s3_blobs import S3BlobStore
from docqa.config import IngestSettings, QASettings
from docqa.domain.chunking import ChunkingConfig
from docqa.pipelines.ingest import IngestService
from docqa.pipelines.qa import QAService


def build_ingest_service(settings: IngestSettings) -> IngestService:
    runtime = bedrock_runtime()
    return IngestService(
        blobs=S3BlobStore(settings.docs_bucket),
        vision=BedrockVisionTranscriber(settings.vision_model_id, runtime),
        metadata=BedrockMetadataExtractor(settings.metadata_model_id, runtime),
        embedder=TitanEmbedder(settings.embedding_model_id, settings.embedding_dimensions, runtime),
        index=LanceChunkIndex(
            settings.resolved_lancedb_uri, settings.index_table, settings.embedding_dimensions
        ),
        chunking=ChunkingConfig(settings.chunk_max_tokens, settings.chunk_overlap_tokens),
    )


def build_qa_service(settings: QASettings, docs_bucket: str) -> QAService:
    runtime = bedrock_runtime()
    return QAService(
        searcher=LanceChunkSearcher(settings.lancedb_uri_for(docs_bucket), settings.index_table),
        embedder=TitanEmbedder(settings.embedding_model_id, settings.embedding_dimensions, runtime),
        embedding_model_id=settings.embedding_model_id,
        generator=BedrockGenerator(settings.generation_model_id, runtime),
        reranker=CohereReranker(settings.rerank_model_id, runtime),
        candidates=settings.candidates,
        top_k=settings.top_k,
        max_answer_tokens=settings.max_answer_tokens,
    )
