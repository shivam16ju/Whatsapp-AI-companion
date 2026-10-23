import pytest
from datetime import datetime
from app.rag import parse_chat_export, chunk_messages


def test_parse_standard_dash_format():
    sample = (
        "12/03/24, 9:41 pm - Alice: Hey what's up?\n"
        "12/03/24, 9:42 pm - Bob: All good, chill kar raha hu bro\n"
        "12/03/24, 9:43 pm - Alice: Nice scene\n"
    )
    messages = parse_chat_export(sample)
    assert len(messages) == 3
    assert messages[0][1] == "Alice"
    assert messages[0][2] == "Hey what's up?"
    assert messages[1][1] == "Bob"
    assert messages[1][2] == "All good, chill kar raha hu bro"
    assert messages[2][1] == "Alice"
    assert messages[2][2] == "Nice scene"


def test_parse_bracket_format():
    sample = (
        "[12/03/2024, 21:41:05] Alice: Kya plan hai aaj?\n"
        "[12/03/2024, 21:42:10] Bob: Kuch nahi yaar, code kar raha hu\n"
    )
    messages = parse_chat_export(sample)
    assert len(messages) == 2
    assert messages[0][1] == "Alice"
    assert messages[0][2] == "Kya plan hai aaj?"
    assert messages[1][1] == "Bob"
    assert messages[1][2] == "Kuch nahi yaar, code kar raha hu"


def test_parse_multiline_messages():
    sample = (
        "12/03/24, 9:41 pm - Alice: Sunna ek baat bolu\n"
        "Yeh bahut lambi story hai\n"
        "Kal hum market gaye the\n"
        "12/03/24, 9:45 pm - Bob: Haan bol sun raha hu\n"
    )
    messages = parse_chat_export(sample)
    assert len(messages) == 2
    assert messages[0][1] == "Alice"
    assert "Yeh bahut lambi story hai\nKal hum market gaye the" in messages[0][2]
    assert messages[1][1] == "Bob"
    assert messages[1][2] == "Haan bol sun raha hu"


def test_parse_skips_media_and_system_lines():
    sample = (
        "12/03/24, 9:40 pm - Messages and calls are end-to-end encrypted. No one outside of this chat can read them.\n"
        "12/03/24, 9:41 pm - Alice: Hey\n"
        "12/03/24, 9:42 pm - Bob: <Media omitted>\n"
        "12/03/24, 9:43 pm - You created group \"Goa Trip\"\n"
        "12/03/24, 9:44 pm - Bob: Check this photo out\n"
        "12/03/24, 9:45 pm - Alice: \u200eimage omitted\n"
        "12/03/24, 9:46 pm - Alice: mast hai\n"
    )
    messages = parse_chat_export(sample)
    assert len(messages) == 3
    assert messages[0][1] == "Alice"
    assert messages[0][2] == "Hey"
    assert messages[1][1] == "Bob"
    assert messages[1][2] == "Check this photo out"
    assert messages[2][1] == "Alice"
    assert messages[2][2] == "mast hai"


def test_chunker_sliding_window_and_owner_filtering():
    now = datetime.now()
    msgs = [
        (now, "Alice", "msg 1"),
        (now, "Bob", "msg 2"),
        (now, "Alice", "msg 3"),
        (now, "Bob", "msg 4"),
        (now, "Alice", "msg 5"),
        (now, "Bob", "msg 6"),
        (now, "Alice", "msg 7"),
        (now, "Charlie", "msg 8"),
    ]

    # Owner is Bob -> Bob participated in windows
    chunks = chunk_messages(msgs, owner_name="Bob", window_size=6, step=3)
    assert len(chunks) > 0
    for chunk in chunks:
        assert "chunk_id" in chunk
        assert "ts" in chunk
        assert "text" in chunk
        assert "Bob:" in chunk["text"]

    # Filter out if owner never participated
    no_chunks = chunk_messages(msgs, owner_name="Dave", window_size=6, step=3)
    assert len(no_chunks) == 0
