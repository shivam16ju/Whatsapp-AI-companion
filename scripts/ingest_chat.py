#!/usr/bin/env python3
import sys
import argparse
from pathlib import Path

# Add project root to sys.path so app imports work when script is called directly
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from app.config import settings
from app.rag import parse_chat_export, chunk_messages, upsert_chunks


def main():
    parser = argparse.ArgumentParser(
        description="Ingest WhatsApp chat export .txt into Pinecone vector DB for style RAG."
    )
    parser.add_argument("file", help="Path to WhatsApp exported chat .txt file")
    parser.add_argument(
        "--me",
        default=settings.OWNER_NAME or "Owner",
        help="Your exact sender name in the chat export (default: settings.OWNER_NAME)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=50,
        help="Embedding and upsert batch size (default: 50)",
    )
    args = parser.parse_args()

    file_path = Path(args.file)
    if not file_path.exists():
        print(f"Error: File '{args.file}' does not exist.")
        sys.exit(1)

    print(f"Reading chat export from '{file_path}'...")
    content = file_path.read_text(encoding="utf-8", errors="replace")

    print("Parsing messages...")
    messages = parse_chat_export(content)
    print(f"Parsed {len(messages)} valid chat messages.")

    if not messages:
        print("No valid messages found. Check file format.")
        sys.exit(0)

    print(f"Chunking with sliding window (owner: '{args.me}')...")
    chunks = chunk_messages(messages, owner_name=args.me)
    print(f"Generated {len(chunks)} chunks containing your responses.")

    if not chunks:
        print(f"Warning: No chunks found where sender was '{args.me}'. Verify your name parameter.")
        sys.exit(0)

    print("Embedding chunks and upserting to Pinecone...")
    upserted = upsert_chunks(chunks, batch_size=args.batch_size)
    print(f"Successfully upserted {upserted} chunks to index '{settings.PINECONE_INDEX}'!")


if __name__ == "__main__":
    main()
