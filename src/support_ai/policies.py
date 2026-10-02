"""Page-aware PDF ingestion and persistent policy retrieval.

This service will sit behind MCP. It does not generate answers or execute any
instructions found in retrieved policy text.
"""

import argparse
import hashlib
import json
import logging
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import chromadb
import pymupdf
from chromadb.config import Settings as ChromaSettings
from openai import OpenAI, OpenAIError

from support_ai.config import PolicySettings, Settings, configure_logging

logger = logging.getLogger(__name__)
MAX_PDF_BYTES = 20 * 1024 * 1024
MAX_PAGES = 200
MAX_CHUNKS = 1000
MAX_PAGE_CHARS = 100_000


class PolicyError(RuntimeError):
    """A safe, user-facing policy operation error."""


class Embedder(Protocol):
    model: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class OpenAIEmbedder:
    def __init__(self, model: str, client=None):
        self.model = model
        self._client = client

    def embed(self, texts: list[str]) -> list[list[float]]:
        if self._client is None:
            if not os.getenv("OPENAI_API_KEY", "").strip():
                raise PolicyError("Set OPENAI_API_KEY in your local .env before embedding or searching.")
            self._client = OpenAI(timeout=30, max_retries=2)
        vectors = []
        try:
            for start in range(0, len(texts), 32):
                batch = texts[start:start + 32]
                response = self._client.embeddings.create(
                    model=self.model, input=batch, encoding_format="float",
                )
                items = sorted(response.data, key=lambda item: item.index)
                if [item.index for item in items] != list(range(len(batch))):
                    raise PolicyError("Embedding service returned an incomplete batch; retry ingestion.")
                vectors.extend(item.embedding for item in items)
        except OpenAIError as exc:
            logger.error("Embedding request failed (%s)", type(exc).__name__)
            raise PolicyError("OpenAI embeddings failed; check your key, quota, and network, then retry.") from exc
        return vectors


@dataclass(frozen=True)
class Chunk:
    text: str
    page: int
    index: int


def extract_chunks(pdf: bytes, settings: PolicySettings) -> list[Chunk]:
    settings.validate()
    if not pdf or len(pdf) > MAX_PDF_BYTES:
        raise PolicyError("PDF must be nonempty and no larger than 20 MiB.")
    if not pdf.startswith(b"%PDF-"):
        raise PolicyError("The upload is not a PDF.")
    chunks = []
    try:
        with pymupdf.open(stream=pdf, filetype="pdf") as document:
            if document.needs_pass:
                raise PolicyError("Password-protected PDFs are not supported; upload an unlocked copy.")
            if not 1 <= len(document) <= MAX_PAGES:
                raise PolicyError("PDF must contain between 1 and 200 pages.")
            for page_number, page in enumerate(document, start=1):
                text = " ".join(page.get_text("text", sort=True).split())
                if len(text) > MAX_PAGE_CHARS:
                    raise PolicyError("A PDF page contains too much text; split the document.")
                if not text:
                    # Reject partial extraction rather than silently omitting scanned policy pages.
                    raise PolicyError(f"Page {page_number} has no extractable text; OCR or remove blank pages first.")
                start = 0
                while start < len(text):
                    end = min(start + settings.chunk_size, len(text))
                    if end < len(text):
                        boundary = text.rfind(" ", start + settings.chunk_size // 2, end)
                        if boundary > start:
                            end = boundary
                    chunks.append(Chunk(text[start:end].strip(), page_number, len(chunks)))
                    if len(chunks) > MAX_CHUNKS:
                        raise PolicyError("PDF produces too many chunks; split it into smaller documents.")
                    if end == len(text):
                        break
                    start = max(start + 1, end - settings.chunk_overlap)
    except PolicyError:
        raise
    except Exception as exc:
        logger.warning("PDF extraction failed (%s)", type(exc).__name__)
        raise PolicyError("PDF could not be read; upload a valid text-based PDF.") from exc
    return chunks


def _vectors(embedder: Embedder, texts: list[str]) -> list[list[float]]:
    vectors = embedder.embed(texts)
    if len(vectors) != len(texts) or not vectors:
        raise PolicyError("Embedding count does not match the input texts.")
    dimension = len(vectors[0])
    if not dimension or any(len(vector) != dimension or
                            not all(math.isfinite(value) for value in vector) or
                            not any(vector) for vector in vectors):
        raise PolicyError("Embedding service returned invalid vectors.")
    return vectors


class PolicyIndex:
    def __init__(self, settings: PolicySettings, embedder: Embedder | None = None):
        settings.validate()
        self.settings = settings
        self.embedder = embedder or OpenAIEmbedder(settings.embedding_model)
        if self.embedder.model != settings.embedding_model:
            raise PolicyError("Embedding model does not match index configuration.")
        metadata = {"embedding_model": self.embedder.model, "schema_version": 1,
                    "chunk_size": settings.chunk_size, "chunk_overlap": settings.chunk_overlap}
        try:
            self.client = chromadb.PersistentClient(
                path=str(settings.chroma_path), settings=ChromaSettings(anonymized_telemetry=False),
            )
            self.collection = self.client.get_or_create_collection(
                settings.collection, embedding_function=None, metadata=metadata,
                configuration={"hnsw": {"space": "cosine"}},
            )
            actual = self.collection.metadata or {}
            if any(actual.get(key) != value for key, value in metadata.items()):
                raise PolicyError("Index configuration changed; select a new CHROMA_COLLECTION and reingest.")
        except PolicyError:
            raise
        except Exception as exc:
            logger.error("Policy index initialization failed (%s)", type(exc).__name__)
            raise PolicyError("Cannot open policy index; check Chroma path and configuration.") from exc

    def ingest_file(self, path: Path) -> dict:
        try:
            with path.open("rb") as stream:
                pdf = stream.read(MAX_PDF_BYTES + 1)
        except OSError as exc:
            raise PolicyError("Cannot read the selected PDF file.") from exc
        return self.ingest_bytes(pdf, path.name)

    def ingest_bytes(self, pdf: bytes, filename: str) -> dict:
        # Basename only: never preserve client paths in metadata or write uploads to them.
        source = filename.replace("\\", "/").rsplit("/", 1)[-1]
        if not source.lower().endswith(".pdf") or len(source) > 200 or any(ord(c) < 32 for c in source):
            raise PolicyError("Provide a PDF filename of at most 200 characters.")
        chunks = extract_chunks(pdf, self.settings)
        document_id = hashlib.sha256(pdf).hexdigest()
        ids = [f"{document_id}:{chunk.index}" for chunk in chunks]
        try:
            existing = self.collection.get(where={"document_id": document_id}, include=["metadatas"])
            if set(existing["ids"]) == set(ids):
                return {"status": "duplicate", "document_id": document_id,
                        "source": existing["metadatas"][0]["source"], "chunks": len(ids)}
            same_name = self.collection.get(where={"source": source}, include=["metadatas"])
            if any(item["document_id"] != document_id for item in same_name["metadatas"]):
                raise PolicyError("A different PDF already uses this filename. Use a new collection for revised policies.")
            vectors = _vectors(self.embedder, [chunk.text for chunk in chunks])
            # All embeddings are obtained before any write. One bounded upsert;
            # deterministic IDs make retrying a failed operation safe.
            self.collection.upsert(
                ids=ids, embeddings=vectors, documents=[chunk.text for chunk in chunks],
                metadatas=[{"source": source, "page": chunk.page, "chunk_index": chunk.index,
                            "document_id": document_id, "embedding_model": self.embedder.model}
                           for chunk in chunks],
            )
            logger.info("Indexed policy document %s (%d chunks)", document_id[:12], len(chunks))
            return {"status": "indexed", "document_id": document_id, "source": source, "chunks": len(ids)}
        except PolicyError:
            raise
        except Exception as exc:
            logger.error("Policy ingestion failed (%s)", type(exc).__name__)
            raise PolicyError("Policy indexing failed; check storage and retry the same PDF.") from exc

    def search(self, query: str, *, limit: int = 5) -> list[dict]:
        if not isinstance(query, str) or not query.strip() or len(query) > 4000:
            raise ValueError("Policy query must contain 1 to 4000 characters")
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("Search limit must be between 1 and 20")
        try:
            count = self.collection.count()
            if not count:
                return []
            result = self.collection.query(
                query_embeddings=_vectors(self.embedder, [query.strip()]), n_results=min(limit, count),
                include=["documents", "metadatas", "distances"],
            )
            return [dict(chunk_id=chunk_id, text=text, **metadata,
                         distance=distance, citation=f"[{metadata['source']}, p. {metadata['page']}]")
                    for chunk_id, text, metadata, distance in zip(
                        result["ids"][0], result["documents"][0], result["metadatas"][0], result["distances"][0])
                    if distance <= self.settings.max_distance]
        except PolicyError:
            raise
        except Exception as exc:
            logger.error("Policy search failed (%s)", type(exc).__name__)
            raise PolicyError("Policy search failed; check storage and embedding configuration.") from exc


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    ingest = commands.add_parser("ingest", help="Embed and persist a PDF")
    ingest.add_argument("pdf", type=Path)
    search = commands.add_parser("search", help="Search indexed policy passages")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()
    try:
        configure_logging(Settings.from_env().log_level)
        index = PolicyIndex(PolicySettings.from_env())
        result = index.ingest_file(args.pdf) if args.command == "ingest" else index.search(args.query, limit=args.limit)
        print(json.dumps(result, indent=2, ensure_ascii=True))
    except (PolicyError, ValueError) as exc:
        parser.exit(1, f"Policy operation failed: {exc}\n")


if __name__ == "__main__":
    main()
