"""Runtime configuration.

Deployed, the app reads one JSON SSM parameter (written by Terraform) at cold start. That
breaks the Terraform cycle between the Function URL, the Cognito client and the function's
environment. Locally, the same values can come from DOCQA_* environment variables instead.
"""

import json
from collections.abc import Callable
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any, Self

import boto3
from pydantic import BaseModel, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DOCQA_")

    environment: str = "local"
    config_parameter: str | None = None
    cognito_domain: str | None = None
    cognito_client_id: str | None = None
    cognito_issuer: str | None = None
    allowed_redirect_uris: list[str] = []
    docs_bucket: str | None = None


class AppConfig(BaseModel):
    environment: str
    cognito_domain: str
    cognito_client_id: str
    cognito_issuer: str
    allowed_redirect_uris: list[str]
    docs_bucket: str | None = None


ParameterReader = Callable[[str], str]


def ssm_parameter_reader(name: str) -> str:
    response: Any = boto3.client("ssm").get_parameter(Name=name)
    return str(response["Parameter"]["Value"])


def load_config(
    settings: Settings, read_parameter: ParameterReader = ssm_parameter_reader
) -> AppConfig:
    if settings.config_parameter:
        values = json.loads(read_parameter(settings.config_parameter))
    else:
        values = settings.model_dump(exclude={"environment", "config_parameter"})
    return AppConfig(environment=settings.environment, **values)


@lru_cache
def get_config() -> AppConfig:
    return load_config(Settings())


class Provider(StrEnum):
    BEDROCK = "bedrock"  # deployed: Bedrock models, S3 documents, LanceDB on S3
    LOCAL = "local"  # laptop: Ollama models, files and LanceDB under local_data_dir, $0


# In local mode, model and table fields default to these (an explicit DOCQA_* value wins).
# The table name differs so a local index never mixes with the Titan one.
LOCAL_DEFAULTS = {
    "index_table": "chunks__struct400__bgem3_1024",
    "embedding_model_id": "bge-m3",
    "vision_model_id": "qwen2.5vl:7b",
    "metadata_model_id": "qwen2.5:7b",
    "generation_model_id": "qwen2.5:7b",
}


class IndexSettings(BaseSettings):
    """Which provider and index variant to use. Shared by ingestion (writes) and Q&A (reads)."""

    model_config = SettingsConfigDict(env_prefix="DOCQA_")

    provider: Provider = Provider.BEDROCK
    ollama_url: str = "http://localhost:11434"
    local_data_dir: Path = Path(".data")  # local mode: raw/, parsed/ and lancedb/ live here
    lancedb_uri: str | None = (
        None  # default: s3://<docs_bucket>/lancedb or <local_data_dir>/lancedb
    )
    index_table: str = "chunks__struct400__titan1024"
    embedding_model_id: str = "amazon.titan-embed-text-v2:0"
    embedding_dimensions: int = 1024

    @model_validator(mode="after")
    def _apply_local_defaults(self) -> Self:
        if self.provider is Provider.LOCAL:
            for name, value in LOCAL_DEFAULTS.items():
                if name in type(self).model_fields and name not in self.model_fields_set:
                    setattr(self, name, value)
        return self

    def lancedb_uri_for(self, bucket: str | None) -> str:
        if self.lancedb_uri:
            return self.lancedb_uri
        if self.provider is Provider.LOCAL:
            return str(self.local_data_dir / "lancedb")
        if not bucket:
            raise ValueError("a documents bucket is required with the bedrock provider")
        return f"s3://{bucket}/lancedb"


class IngestSettings(IndexSettings):
    """Ingestion settings. Bedrock model IDs are cross-region inference profiles."""

    docs_bucket: str | None = None  # required with the bedrock provider
    vision_model_id: str = "us.amazon.nova-2-lite-v1:0"
    metadata_model_id: str = "us.amazon.nova-micro-v1:0"
    chunk_max_tokens: int = 400
    chunk_overlap_tokens: int = 60

    @model_validator(mode="after")
    def _require_bucket_for_bedrock(self) -> Self:
        if self.provider is Provider.BEDROCK and not self.docs_bucket:
            raise ValueError("DOCQA_DOCS_BUCKET is required with the bedrock provider")
        return self

    @property
    def resolved_lancedb_uri(self) -> str:
        return self.lancedb_uri_for(self.docs_bucket)


class QASettings(IndexSettings):
    """Question answering: retrieval depth, models, limits."""

    generation_model_id: str = "us.amazon.nova-micro-v1:0"
    rerank_model_id: str = "cohere.rerank-v3-5:0"
    candidates: int = 20  # per retriever, before fusion/rerank
    top_k: int = 5  # chunks given to the model
    max_answer_tokens: int = 600
