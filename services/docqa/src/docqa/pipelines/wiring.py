"""Builds the real ingestion service from settings (composition root)."""

from docqa.adapters.bedrock import (
    BedrockMetadataExtractor,
    BedrockVisionTranscriber,
    TitanEmbedder,
    bedrock_runtime,
)
from docqa.adapters.lancedb_index import LanceChunkIndex
from docqa.adapters.s3_blobs import S3BlobStore
from docqa.config import IngestSettings
from docqa.domain.chunking import ChunkingConfig
from docqa.pipelines.ingest import IngestService


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
