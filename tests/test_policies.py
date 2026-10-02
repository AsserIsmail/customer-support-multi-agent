import hashlib
import subprocess
import sys
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pymupdf
import pytest
from openai import AuthenticationError

from support_ai.config import PolicySettings, Settings
from support_ai.demo_pdfs import generate_demo_pdf
from support_ai.policies import (
    MAX_PDF_BYTES, OpenAIEmbedder, PolicyError, PolicyIndex, extract_chunks,
)


class FakeEmbedder:
    """Deterministic concept vectors for plumbing tests, not semantic validation."""
    model = "test-vectors"

    def __init__(self):
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        return [[float("refund" in text.lower()), float("shipping" in text.lower()),
                 float("warranty" in text.lower()), 0.01] for text in texts]


def pdf_bytes(*pages):
    with pymupdf.open() as doc:
        for text in pages:
            doc.new_page().insert_textbox(pymupdf.Rect(30, 30, 560, 800), text, fontsize=10)
        return doc.tobytes()


@pytest.fixture
def settings(tmp_path):
    return PolicySettings(tmp_path / "chroma", embedding_model="test-vectors")


@pytest.fixture
def index(settings):
    return PolicyIndex(settings, FakeEmbedder())


def test_page_chunks_preserve_content_and_overlap(settings):
    settings = replace(settings, chunk_size=100, chunk_overlap=20)
    text = " ".join(f"word{number}" for number in range(100))
    chunks = extract_chunks(pdf_bytes(text, "Shipping on page two."), settings)
    assert {chunk.page for chunk in chunks} == {1, 2}
    assert all(0 < len(chunk.text) <= 100 for chunk in chunks)
    assert [chunk.index for chunk in chunks] == list(range(len(chunks)))
    first_page = [chunk.text for chunk in chunks if chunk.page == 1]
    assert first_page[0][-20:].strip() in first_page[1]
    assert all(word in " ".join(first_page) for word in text.split())
    assert chunks[-1].text == "Shipping on page two."


@pytest.mark.parametrize("data, message", [(b"", "nonempty"), (b"bad", "not a PDF"),
    (b"%PDF-broken", "could not be read"), (b"a" * (MAX_PDF_BYTES + 1), "20 MiB")],
    ids=["empty", "not-pdf", "corrupt", "oversized"])
def test_bad_uploads(settings, data, message):
    with pytest.raises(PolicyError, match=message):
        extract_chunks(data, settings)


def test_blank_or_scanned_page_rejected(settings):
    with pytest.raises(PolicyError, match="Page 2"):
        extract_chunks(pdf_bytes("Refund policy", ""), settings)


def test_encrypted_pdf_rejected(settings):
    with pymupdf.open(stream=pdf_bytes("Secret policy"), filetype="pdf") as doc:
        data = doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="reader")
    with pytest.raises(PolicyError, match="Password-protected"):
        extract_chunks(data, settings)


def test_ingest_search_duplicate_and_reopen(index, settings):
    data = pdf_bytes("Refund within 30 days.", "Shipping within 7 days.")
    result = index.ingest_bytes(data, "C:\\uploads\\policy.pdf")
    assert result["status"] == "indexed"
    assert result["chunks"] == 2
    assert result["document_id"] == hashlib.sha256(data).hexdigest()
    assert index.embedder.calls == 1
    assert index.ingest_bytes(data, "renamed.pdf")["source"] == "policy.pdf"
    assert index.embedder.calls == 1
    reopened = PolicyIndex(settings, FakeEmbedder())
    results = reopened.search("refund")
    assert len(results) == 1
    assert results[0]["page"] == 1
    assert results[0]["citation"] == "[policy.pdf, p. 1]"
    assert results[0]["document_id"] == result["document_id"]
    assert reopened.search("warranty") == []
    with pytest.raises(PolicyError, match="different PDF"):
        index.ingest_bytes(pdf_bytes("Refund within 60 days."), "policy.pdf")
    assert index.collection.count() == 2


def test_empty_index_needs_no_key(index):
    assert index.search("refund") == []
    assert index.embedder.calls == 0


def test_chroma_persists_across_processes(index, settings):
    index.ingest_bytes(pdf_bytes("Refund within 30 days."), "refunds.pdf")
    code = """
import sys
import chromadb
from chromadb.config import Settings
client = chromadb.PersistentClient(path=sys.argv[1], settings=Settings(anonymized_telemetry=False))
collection = client.get_collection('company_policies', embedding_function=None)
assert collection.count() == 1
result = collection.query(query_embeddings=[[1.0, 0.0, 0.0, 0.01]], n_results=1)
assert result['metadatas'][0][0]['source'] == 'refunds.pdf'
assert result['metadatas'][0][0]['page'] == 1
print('persisted')
"""
    result = subprocess.run([sys.executable, "-c", code, str(settings.chroma_path)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert "persisted" in result.stdout


def test_embedding_failure_leaves_index_unchanged(index, monkeypatch):
    def fail(texts):
        raise PolicyError("Fake provider outage")
    monkeypatch.setattr(index.embedder, "embed", fail)
    with pytest.raises(PolicyError, match="outage"):
        index.ingest_bytes(pdf_bytes("Refund policy"), "policy.pdf")
    assert index.collection.count() == 0


@pytest.mark.parametrize("vectors", [[], [[]], [[0, 0]], [[float("nan"), 1]]])
def test_invalid_embeddings_are_not_saved(index, monkeypatch, vectors):
    monkeypatch.setattr(index.embedder, "embed", lambda texts: vectors)
    with pytest.raises(PolicyError, match="Embedding"):
        index.ingest_bytes(pdf_bytes("Refund policy"), "policy.pdf")
    assert index.collection.count() == 0


def test_changed_index_configuration_rejected(index, settings):
    with pytest.raises(PolicyError, match="configuration changed"):
        PolicyIndex(replace(settings, chunk_size=1000), FakeEmbedder())
    embedder = FakeEmbedder()
    embedder.model = "other-model"
    with pytest.raises(PolicyError, match="configuration changed"):
        PolicyIndex(replace(settings, embedding_model="other-model"), embedder)


@pytest.mark.parametrize("query, limit", [("", 5), ("x" * 4001, 5), (None, 5),
                                        ("refund", 0), ("refund", True), ("refund", 21)])
def test_invalid_queries(index, query, limit):
    with pytest.raises(ValueError):
        index.search(query, limit=limit)


def test_openai_adapter_batching_and_order():
    requests = []
    def create(**kwargs):
        requests.append(kwargs)
        return SimpleNamespace(data=[SimpleNamespace(index=i, embedding=[float(i), 1.0])
                                     for i in reversed(range(len(kwargs["input"])))])
    adapter = OpenAIEmbedder("example-model", SimpleNamespace(embeddings=SimpleNamespace(create=create)))
    result = adapter.embed(["policy"] * 33)
    assert len(result) == 33
    assert [len(request["input"]) for request in requests] == [32, 1]
    assert requests[0]["model"] == "example-model"
    assert result[:2] == [[0.0, 1.0], [1.0, 1.0]]


def test_openai_error_redacts_provider_details(caplog):
    def create(**kwargs):
        raise AuthenticationError("sensitive-provider-detail", response=httpx.Response(
            401, request=httpx.Request("POST", "https://api.openai.com/v1/embeddings")), body=None)
    adapter = OpenAIEmbedder("example", SimpleNamespace(embeddings=SimpleNamespace(create=create)))
    with pytest.raises(PolicyError) as caught:
        adapter.embed(["private policy"])
    assert "sensitive-provider-detail" not in str(caught.value) + caplog.text
    assert "private policy" not in caplog.text


def test_missing_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(PolicyError, match="OPENAI_API_KEY"):
        OpenAIEmbedder("text-embedding-3-small").embed(["refund"])


def test_demo_is_deterministic_and_has_three_pages(tmp_path, settings):
    first, second = tmp_path / "first.pdf", tmp_path / "second.pdf"
    generate_demo_pdf(first)
    generate_demo_pdf(second)
    assert first.read_bytes() == second.read_bytes()
    chunks = extract_chunks(first.read_bytes(), settings)
    assert {chunk.page for chunk in chunks} == {1, 2, 3}
    assert "30 calendar days" in " ".join(chunk.text for chunk in chunks)


def test_dotenv_precedence_and_policy_config(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("SUPPORT_LOG_LEVEL=DEBUG\nCHROMA_PATH=local-index\n", encoding="utf-8")
    monkeypatch.delenv("CHROMA_PATH", raising=False)
    monkeypatch.setenv("SUPPORT_LOG_LEVEL", "WARNING")
    assert Settings.from_env().log_level == "WARNING"
    assert PolicySettings.from_env().chroma_path == tmp_path / "local-index"
    monkeypatch.setenv("POLICY_CHUNK_OVERLAP", "9999")
    with pytest.raises(ValueError, match="overlap"):
        PolicySettings.from_env()
