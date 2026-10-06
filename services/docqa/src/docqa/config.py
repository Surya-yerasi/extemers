"""Runtime configuration.

Deployed, the app reads one JSON SSM parameter (written by Terraform) at cold start. That
breaks the Terraform cycle between the Function URL, the Cognito client and the function's
environment. Locally, the same values can come from DOCQA_* environment variables instead.
"""

import json
from collections.abc import Callable
from functools import lru_cache
from typing import Any

import boto3
from pydantic import BaseModel
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


class IndexSettings(BaseSettings):
    """Which index variant to use. Shared by ingestion (writes) and Q&A (reads)."""

    model_config = SettingsConfigDict(env_prefix="DOCQA_")

    lancedb_uri: str | None = None  # default: s3://<docs_bucket>/lancedb
    index_table: str = "chunks__struct400__titan1024"
    embedding_model_id: str = "amazon.titan-embed-text-v2:0"
    embedding_dimensions: int = 1024

    def lancedb_uri_for(self, bucket: str) -> str:
        return self.lancedb_uri or f"s3://{bucket}/lancedb"


class IngestSettings(IndexSettings):
    """Ingestion settings. Model IDs are cross-region inference profiles."""

    docs_bucket: str
    vision_model_id: str = "us.amazon.nova-2-lite-v1:0"
    metadata_model_id: str = "us.amazon.nova-micro-v1:0"
    chunk_max_tokens: int = 400
    chunk_overlap_tokens: int = 60

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
