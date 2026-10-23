import re
import hashlib
import logging
from datetime import datetime, timezone
from typing import List, Tuple, Dict, Any, Optional
from dateutil import parser as dt_parser

from google import genai
from google.genai import types
from pinecone import Pinecone, ServerlessSpec

from app.config import settings

logger = logging.getLogger(__name__)

# Pattern for [12/03/2024, 21:41:05] Name: text
BRACKET_PATTERN = re.compile(
    r"^\[(\d{1,4}[/\-\.]\d{1,2}[/\-\.]\d{1,4},\s*\d{1,2}:\d{2}(?::\d{2})?(?:\s*[apAP]\.?[mM]\.?)?)\]\s+([^:]+?):\s+(.*)$"
)

# Pattern for 12/03/24, 9:41 pm - Name: text
DASH_PATTERN = re.compile(
    r"^(\d{1,4}[/\-\.]\d{1,2}[/\-\.]\d{1,4},\s*\d{1,2}:\d{2}(?::\d{2})?(?:\s*[apAP]\.?[mM]\.?)?)\s*[-–]\s+([^:]+?):\s+(.*)$"
)

MEDIA_OMITTED_KEYWORDS = [
    "<media omitted>",
    "image omitted",
    "video omitted",
    "audio omitted",
    "sticker omitted",
    "gif omitted",
    "contact card omitted",
    "document omitted",
]


def clean_text_line(text: str) -> str:
    """Clean invisible unicode marks and normalize whitespace."""
    # Strip unicode control characters like left-to-right marks (\u200e, \u200f) and non-breaking spaces
    cleaned = text.replace("\u200e", "").replace("\u200f", "")
    cleaned = cleaned.replace("\u202f", " ").replace("\xa0", " ")
    return cleaned


def is_media_omitted(text: str) -> bool:
    """Check if message is a media omitted placeholder."""
    cleaned = clean_text_line(text).strip().lower()
    return any(k in cleaned for k in MEDIA_OMITTED_KEYWORDS)


def is_system_line(line: str) -> bool:
    """Check if line is an encrypted notice or system status line without sender colon."""
    cleaned = clean_text_line(line).strip().lower()
    system_phrases = [
        "messages and calls are end-to-end encrypted",
        "created group",
        "changed the group",
        "added you",
        "left the group",
        "security code changed",
        "this message was deleted",
    ]
    return any(p in cleaned for p in system_phrases)


def parse_chat_export(text: str) -> List[Tuple[datetime, str, str]]:
    """
    Parse WhatsApp .txt chat export into a list of (timestamp, sender, text).
    Supports both formats:
    - 12/03/24, 9:41 pm - Name: text
    - [12/03/2024, 21:41:05] Name: text
    Handles multi-line messages and filters out system lines and <Media omitted>.
    """
    results: List[Tuple[datetime, str, str]] = []
    lines = text.splitlines()

    current_ts: Optional[datetime] = None
    current_sender: Optional[str] = None
    current_text_parts: List[str] = []

    def flush_current():
        nonlocal current_ts, current_sender, current_text_parts
        if current_ts and current_sender and current_text_parts:
            full_text = "\n".join(current_text_parts).strip()
            if full_text and not is_media_omitted(full_text):
                results.append((current_ts, current_sender, full_text))
        current_ts = None
        current_sender = None
        current_text_parts = []

    for raw_line in lines:
        line = clean_text_line(raw_line)
        if not line.strip():
            if current_text_parts:
                current_text_parts.append("")
            continue

        # Check bracket format
        match = BRACKET_PATTERN.match(line)
        if not match:
            # Check dash format
            match = DASH_PATTERN.match(line)

        if match:
            raw_ts, raw_sender, initial_text = match.groups()
            raw_sender = raw_sender.strip()
            if is_system_line(raw_sender) or is_system_line(initial_text):
                flush_current()
                continue

            flush_current()

            # Parse datetime
            try:
                parsed_dt = dt_parser.parse(raw_ts, fuzzy=True)
            except Exception:
                parsed_dt = datetime.now(timezone.utc)

            current_ts = parsed_dt
            current_sender = raw_sender
            current_text_parts = [initial_text.strip()]
        else:
            # Check if this is an un-prefixed system line
            if is_system_line(line):
                flush_current()
                continue
            # Multi-line message continuation
            if current_text_parts:
                current_text_parts.append(line)

    flush_current()
    return results


def chunk_messages(
    messages: List[Tuple[datetime, str, str]],
    owner_name: str,
    window_size: int = 6,
    step: int = 3,
) -> List[Dict[str, Any]]:
    """
    Build chunks as sliding windows of 6 consecutive messages, step 3.
    Only keep windows where OWNER wrote at least one message.
    Chunk text format: one line per message, 'Sender: text'.
    Returns list of dicts: {chunk_id, ts, text}
    """
    chunks: List[Dict[str, Any]] = []
    if not messages:
        return chunks

    clean_owner = owner_name.strip().lower()
    n = len(messages)

    for i in range(0, n, step):
        window = messages[i : i + window_size]
        if not window:
            continue

        # Check if owner wrote at least one message in this window
        has_owner = any(s.strip().lower() == clean_owner for _, s, _ in window)
        if not has_owner:
            continue

        chunk_lines = [f"{s}: {t}" for _, s, t in window]
        chunk_text = "\n".join(chunk_lines)

        chunk_hash = hashlib.sha256(chunk_text.encode("utf-8")).hexdigest()[:16]
        chunk_id = f"chunk-{chunk_hash}"
        ts_iso = window[-1][0].isoformat()

        chunks.append({
            "chunk_id": chunk_id,
            "ts": ts_iso,
            "text": chunk_text,
        })

    return chunks


def get_genai_client() -> genai.Client:
    """Return initialized Google GenAI client."""
    if not settings.GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY is not configured for embedding")
    return genai.Client(api_key=settings.GEMINI_API_KEY)


def embed_texts(texts: List[str]) -> List[List[float]]:
    """Generate vector embeddings for a list of texts using Gemini embedding model."""
    if not texts:
        return []

    client = get_genai_client()
    model_name = "gemini-embedding-001"
    config = types.EmbedContentConfig(output_dimensionality=settings.EMBED_DIM)

    response = client.models.embed_content(
        model=model_name,
        contents=texts,
        config=config,
    )

    embeddings = []
    for item in response.embeddings:
        embeddings.append(item.values)
    return embeddings


def get_pinecone_index() -> Any:
    """Initialize Pinecone client and return Index instance, creating index if needed."""
    if not settings.PINECONE_API_KEY:
        raise ValueError("PINECONE_API_KEY is not configured")

    pc = Pinecone(api_key=settings.PINECONE_API_KEY)
    index_name = settings.PINECONE_INDEX

    existing_indexes = [idx.name for idx in pc.list_indexes()]
    if index_name not in existing_indexes:
        logger.info("Creating Pinecone index '%s' (dim: %d, metric: cosine)...", index_name, settings.EMBED_DIM)
        pc.create_index(
            name=index_name,
            dimension=settings.EMBED_DIM,
            metric="cosine",
            spec=ServerlessSpec(cloud="aws", region="us-east-1"),
        )

    return pc.Index(index_name)


def upsert_chunks(chunks: List[Dict[str, Any]], batch_size: int = 50) -> int:
    """
    Embed chunks in batches of 50 and upsert to Pinecone with ids 'chunk-<hash>'.
    Idempotent re-ingestion. Returns total chunks upserted.
    """
    if not chunks:
        return 0

    index = get_pinecone_index()
    total_upserted = 0

    for i in range(0, len(chunks), batch_size):
        batch = chunks[i : i + batch_size]
        texts = [c["text"] for c in batch]
        embeddings = embed_texts(texts)

        vectors = []
        for c, emb in zip(batch, embeddings):
            vectors.append({
                "id": c["chunk_id"],
                "values": emb,
                "metadata": {
                    "chunk_id": c["chunk_id"],
                    "ts": c["ts"],
                    "text": c["text"],
                },
            })

        index.upsert(vectors=vectors)
        total_upserted += len(vectors)
        logger.info("Upserted batch of %d chunks to Pinecone (total: %d)", len(vectors), total_upserted)

    return total_upserted


_pinecone_auth_failed = False

def query_chunks(query_text: str, top_k: int = 6, score_threshold: float = 0.55) -> List[str]:
    """
    Embed incoming message, query Pinecone top_k=6, drop matches with score < 0.55.
    Returns chunk texts ordered by score.
    Returns empty list on any Pinecone error (ensuring the bot still replies).
    """
    global _pinecone_auth_failed
    if not query_text or not query_text.strip() or _pinecone_auth_failed:
        return []

    try:
        embeddings = embed_texts([query_text])
        if not embeddings:
            return []
        query_vector = embeddings[0]

        index = get_pinecone_index()
        query_res = index.query(
            vector=query_vector,
            top_k=top_k,
            include_metadata=True,
        )

        matches = query_res.get("matches", []) if isinstance(query_res, dict) else getattr(query_res, "matches", [])
        matched_chunks = []
        for m in matches:
            score = m.get("score") if isinstance(m, dict) else getattr(m, "score", 0.0)
            if score >= score_threshold:
                metadata = m.get("metadata", {}) if isinstance(m, dict) else getattr(m, "metadata", {})
                chunk_str = metadata.get("text", "")
                if chunk_str:
                    matched_chunks.append(chunk_str)

        return matched_chunks

    except Exception as e:
        err_msg = str(e).lower()
        if "401" in err_msg or "unauthorized" in err_msg or "invalid api key" in err_msg:
            _pinecone_auth_failed = True
            logger.warning("Pinecone API key invalid or unauthorized; disabling RAG style lookup for instant performance.")
        else:
            logger.warning("Pinecone query failed (falling back gracefully): %s", e)
        return []
