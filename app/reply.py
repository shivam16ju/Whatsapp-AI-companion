import re
import random
import logging
from typing import Optional, List, Dict, Tuple
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Contact, Message, Memory
from app.llm import get_llm
from app.image_gen import is_photo_request, build_photo_prompt, generate_image

logger = logging.getLogger(__name__)

# Verified looping Giphy MP4 presets (native WhatsApp GIF playback)
GIF_PRESETS: Dict[str, List[str]] = {
    "herapheri": [
        "https://media.giphy.com/media/Nm8ZPAGOwZUQM/giphy.mp4",
        "https://media.giphy.com/media/26n6Gx9moCgs1pUuk/giphy.mp4",
    ],
    "jethalal": [
        "https://media.giphy.com/media/l3fQf1OEAq0iri9RC/giphy.mp4",
        "https://media.giphy.com/media/Nm8ZPAGOwZUQM/giphy.mp4",
    ],
    "meme": [
        "https://media.giphy.com/media/gTURHJs4e2Ies/giphy.mp4",
        "https://media.giphy.com/media/l3q2K5jinAlChoCLS/giphy.mp4",
        "https://media.giphy.com/media/kC8N6DPOkbqWTxkNTe/giphy.mp4",
    ],
    "roast": [
        "https://media.giphy.com/media/3o85xnoIXebk3xYx4Q/giphy.mp4",  # Idiot sandwich
        "https://media.giphy.com/media/dEdmW17JnZhiU/giphy.mp4",        # Eye roll
    ],
    "psycho": [
        "https://media.giphy.com/media/3o7TKSxdQJIoiRXHl6/giphy.mp4",  # Dark laugh
    ],
    "flirty": [
        "https://media.giphy.com/media/B0vFTrb0ZGDf2/giphy.mp4",        # Smirk
    ],
    "bruh": [
        "https://media.giphy.com/media/ISOckXUybVfQ4/giphy.mp4",        # Stare blank
    ],
}

# Verified WebP WhatsApp Sticker presets
STICKER_PRESETS: Dict[str, List[str]] = {
    "smile": ["https://raw.githubusercontent.com/WhatsApp/stickers/main/Android/app/src/main/assets/1/01_Cuppy_smile.webp"],
    "laugh": ["https://raw.githubusercontent.com/WhatsApp/stickers/main/Android/app/src/main/assets/1/02_Cuppy_lol.webp"],
    "rofl": ["https://raw.githubusercontent.com/WhatsApp/stickers/main/Android/app/src/main/assets/1/03_Cuppy_rofl.webp"],
    "sad": ["https://raw.githubusercontent.com/WhatsApp/stickers/main/Android/app/src/main/assets/1/04_Cuppy_sad.webp"],
}


def is_detailed_request(text: str) -> bool:
    """Check if the user is asking for tips, trips, advice, planning, or a detailed query."""
    keywords = [
        "tip", "tips", "trip", "trips", "travel", "plan", "batao", "bata na",
        "kaise", "kya karu", "suggest", "recommend", "recommendation", "advice",
        "kaha jau", "kaha ghumu", "itinerary", "guide", "explain", "detail",
        "help", "options", "best jagah", "route", "budget", "kya dekhu"
    ]
    lower = text.lower()
    return any(k in lower for k in keywords) or len(text.strip().split()) > 14


def is_devanagari_hindi(text: str) -> bool:
    """Check if text contains Hindi Devanagari script."""
    return bool(re.search(r"[\u0900-\u097F]", text))


def detect_relationship_mode(contact: Contact, incoming_text: str, history: List[Dict[str, str]]) -> str:
    """
    Detect the relationship and communication dynamic based on contact info and incoming message:
    Returns one of:
    - 'CUSTOM_OVERRIDE' (When custom persona notes are defined)
    - 'ELDER_FAMILY' (Mummy, Papa, elders, people saying 'beta'/'bachha')
    - 'ROMANTIC_PARTNER' (Girlfriend, boyfriend, romantic partner)
    - 'FORMAL_PROFESSIONAL' (Boss, client, formal colleague)
    - 'DECENT_POLITE' (Polite, courteous, formal, 'Aap', 'Hello', 'Please')
    - 'SAVAGE_ROAST' (Only when insulted/abused first with foul language)
    - 'CASUAL_FRIEND' (Default for peers, bros, casual Hinglish chat)
    """
    notes = (contact.persona_notes or "").strip().lower()
    name_lower = (contact.name or "").lower()
    text_lower = incoming_text.lower()

    # 1. Custom Persona Notes (Highest priority)
    if notes:
        if any(k in notes for k in ["mummy", "mom", "maa", "mata", "papa", "dad", "elder", "parent"]):
            return "ELDER_FAMILY"
        if any(k in notes for k in ["romantic", "girlfriend", "gf", "boyfriend", "bf", "love", "jaan", "babu", "wife", "dating"]):
            return "ROMANTIC_PARTNER"
        if any(k in notes for k in ["boss", "formal", "professional", "client", "office", "sir"]):
            return "FORMAL_PROFESSIONAL"
        if any(k in notes for k in ["roast", "savage", "bully", "abuse"]):
            return "SAVAGE_ROAST"
        return "CUSTOM_OVERRIDE"

    # 2. Elder / Parent / Family detection
    elder_keywords = [
        "mummy", "mom", "maa", "mata", "papa", "dad", "father", "mother",
        "uncle", "aunty", "chacha", "chachi", "bua", "fufa", "dadi", "dada",
        "nani", "nana", "elder", "family", "parent", "devi", "ammi", "abba"
    ]
    parental_phrases = [
        "beta", "bete", "bachha", "kaha ho beta", "ghar kab", "khana khaya beta",
        "so jao beta", "ashirwad", "pranam", "charan sparsh", "ghar aao", "dhyan rakhna"
    ]

    if any(k in name_lower for k in elder_keywords):
        return "ELDER_FAMILY"
    if any(p in text_lower for p in parental_phrases):
        return "ELDER_FAMILY"

    for h in history[-3:]:
        if h.get("role") == "user":
            h_text = h.get("content", "").lower()
            if any(p in h_text for p in parental_phrases):
                return "ELDER_FAMILY"

    # 3. Provoked / Rude / Toxic Attack (Only roast if attacked first!)
    abusive_keywords = [
        "chutiya", "madarchod", "behenchod", "bhadwe", "bhosdike", "randi",
        "gand", "kamina", "harami", "aukat", "kutti", "kutta", "lodu", "lawde",
        "fuck off", "bastard"
    ]
    if any(a in text_lower for a in abusive_keywords):
        return "SAVAGE_ROAST"

    # 4. Decent / Polite / Respectful
    polite_markers = [
        "aap", "kripya", "namaste", "pranam", "please", "sir", "madam",
        "hello shivam", "hope you are well", "good morning", "good evening",
        "kya aap", "bata sakte hain", "dhanyawad", "thank you"
    ]
    if any(m in text_lower for m in polite_markers):
        return "DECENT_POLITE"

    # 5. Default: Casual friend
    return "CASUAL_FRIEND"


def clean_reply(text: str, is_detailed: bool = False) -> str:
    """
    Post-process LLM response:
    - Strip surrounding quotes and markdown code blocks
    - Normalize whitespace
    - Allow up to 3000 chars for detailed trip/tip queries, 400 chars for regular chats
    """
    if not text:
        return ""

    cleaned = text.strip()
    cleaned = re.sub(r"```.*?```", "", cleaned, flags=re.DOTALL)
    cleaned = re.sub(r"^#+\s*", "", cleaned)
    cleaned = cleaned.strip("\"'“”«»`")
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = cleaned.strip()

    max_len = 3000 if is_detailed else 400
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip()
    return cleaned


def extract_media_and_clean_text(
    raw_text: str,
    incoming_text: str,
    contact: Contact,
    rel_mode: str,
    is_detailed: bool,
) -> Tuple[str, Optional[str], Optional[str], Optional[str]]:
    """
    Extract [PHOTO: <prompt>], [GIF: <category>], and [STICKER: <category>] tags.
    Returns: (cleaned_text, gif_url, sticker_url, image_url)
    """
    if not raw_text:
        return "", None, None, None

    image_url = None
    gif_url = None
    sticker_url = None
    cleaned = raw_text

    # 1. Extract [PHOTO: ...] tag if model generated one
    photo_match = re.search(r"\[PHOTO:\s*(.+?)\]", cleaned, re.IGNORECASE | re.DOTALL)
    if photo_match:
        photo_prompt = photo_match.group(1).strip()
        cleaned = re.sub(r"\[PHOTO:\s*(.+?)\]", "", cleaned, flags=re.IGNORECASE | re.DOTALL).strip()
        image_url = generate_image(photo_prompt)

    # 2. Extract GIF tag
    gif_match = re.search(r"\[GIF:\s*([a-zA-Z0-9_\-]+)\]", cleaned, re.IGNORECASE)
    if gif_match:
        cat = gif_match.group(1).lower().strip()
        cleaned = re.sub(r"\[GIF:\s*([a-zA-Z0-9_\-]+)\]", "", cleaned, flags=re.IGNORECASE).strip()
        candidates = GIF_PRESETS.get(cat) or GIF_PRESETS.get("meme")
        if candidates:
            gif_url = random.choice(candidates)

    # 3. Extract Sticker tag
    stk_match = re.search(r"\[STICKER:\s*([a-zA-Z0-9_\-]+)\]", cleaned, re.IGNORECASE)
    if stk_match:
        cat = stk_match.group(1).lower().strip()
        cleaned = re.sub(r"\[STICKER:\s*([a-zA-Z0-9_\-]+)\]", "", cleaned, flags=re.IGNORECASE).strip()
        candidates = STICKER_PRESETS.get(cat) or STICKER_PRESETS.get("smile")
        if candidates:
            sticker_url = random.choice(candidates)

    cleaned = clean_reply(cleaned, is_detailed=is_detailed)

    # 4. Deterministic photo backup: If user asked for photo/selfie, but no image was created yet
    if not image_url and is_photo_request(incoming_text):
        auto_prompt = build_photo_prompt(incoming_text, contact, rel_mode)
        image_url = generate_image(auto_prompt)
        refusal_phrases = ["cannot send photo", "can't send photo", "ai assistant", "as an ai", "photo nahi bhej", "send nahi kar sakti", "send nahi kar sakta"]
        if not cleaned or any(rp in cleaned.lower() for rp in refusal_phrases):
            if rel_mode == "ROMANTIC_PARTNER":
                cleaned = "Ye lo meri photo baby ❤️ Kaisi lag rahi hu? 🥰"
            elif is_devanagari_hindi(incoming_text):
                cleaned = "ये लो फोटो! 😊"
            else:
                cleaned = "Ye lo photo! Dekh ke batao kaisa hai ✨"

    # If image is attached, do not send GIF/sticker simultaneously
    if image_url:
        gif_url = None
        sticker_url = None
    else:
        # Check if the user explicitly asked for GIF/sticker media
        user_explicitly_wanted_media = any(
            w in incoming_text.lower()
            for w in ["meme", "gif", "sticker", "stiker", "reaction"]
        )

        # Elders / Parents / Formal Contacts: NEVER send media
        if rel_mode in ["ELDER_FAMILY", "FORMAL_PROFESSIONAL", "DECENT_POLITE"]:
            gif_url = None
            sticker_url = None
        elif not user_explicitly_wanted_media:
            # Real human rule: Unless asked, keep 92%+ of messages PURE TEXT
            if random.random() >= 0.08:
                gif_url = None
                sticker_url = None

    return cleaned, gif_url, sticker_url, image_url


def build_system_prompt(
    contact: Contact,
    incoming_text: str,
    history: List[Dict[str, str]],
    style_chunks: List[str],
    facts: List[str],
) -> str:
    """Assemble dynamically customized system prompt based on contact, relation, script, and persona notes."""
    owner_name = settings.OWNER_NAME or "Shivam"
    rel_mode = detect_relationship_mode(contact, incoming_text, history)
    in_hindi_script = is_devanagari_hindi(incoming_text)
    is_detailed = is_detailed_request(incoming_text)
    persona_notes = (contact.persona_notes or "").strip()

    system_parts = []
    system_parts.append(
        f"You are {owner_name} texting directly on your personal WhatsApp. You are a real human, NEVER an AI assistant.\n"
    )

    # 1. SUPREME DIRECTIVE: Custom Persona Notes (if set in Dashboard)
    if persona_notes:
        system_parts.append(f"""
======================================================
🚨 SUPREME PRIORITY DIRECTIVE — CUSTOM CONTACT PERSONA:
You are chatting with "{contact.name or contact.wa_number}".
The owner ({owner_name}) has explicitly configured these custom persona instructions for this contact in the dashboard:
\"\"\"
{persona_notes}
\"\"\"
CRITICAL RULE:
You MUST adopt, follow, and strictly embody the exact personality, tone, vocabulary, language, relationship dynamic, and instructions given above!
These custom persona notes have 100% HIGHEST PRIORITY and OVERRIDE any generic default behavior.
======================================================
""".strip())

    # 2. Relationship / Mode Specific Instructions
    if rel_mode == "ELDER_FAMILY":
        system_parts.append("""
### 👑 MODE: ELDERLY / FAMILY / PARENTS (MUMMY, PAPA, RELATIVES)
- TONE: MAXIMUM RESPECT, LOVE, SWEETNESS, AND OBEDIENCE.
- VOCABULARY: Always use respectful Hindi/Hinglish: 'Aap', 'Ji', 'Haan mummy', 'Ji bas thodi der me aa raha hu', 'Haanji khana khaa liya'.
- ABSOLUTELY FORBIDDEN:
  * NEVER use abuses, rude words, slangs, or roasts (NO 'abey', 'chup', 'bhaag', 'aukat').
  * NEVER use dark humor, psycho stuff, or creepy jokes.
  * NO NSFW, double-meaning, or adult jokes.
  * NO meme GIFs or stickers!
- Examples:
  * 'Beta kaha ho?' -> 'Ji mummy, bas dost ke sath hu, thodi der me ghar aa raha hu.'
  * 'Khana khaya?' -> 'Haanji mummy, khaa liya. Aapne khaya?'
  * 'Ghar kab aaoge?' -> 'Ji bas aadhe ghante me pahunch raha hu.'
""".strip())
    elif rel_mode == "ROMANTIC_PARTNER":
        system_parts.append("""
### ❤️ MODE: ROMANTIC PARTNER / GIRLFRIEND / BOYFRIEND
- TONE: Loving, caring, affectionate, playful, romantic, warm.
- VOCABULARY: Sweet pet names ('jaan', 'babu', 'sweetheart', 'meri jaan'), cute emojis (❤️, 🥰, 😘, ✨).
- Be sweet, attentive, loving, and supportive.
""".strip())
    elif rel_mode == "FORMAL_PROFESSIONAL":
        system_parts.append("""
### 💼 MODE: FORMAL / PROFESSIONAL / WORK
- TONE: Professional, crisp, respectful, clear, and courteous.
- Address respectfully ('Sir', 'Aap', polite professional language).
- NO casual slangs, no memes, no unprovoked rudeness.
""".strip())
    elif rel_mode == "DECENT_POLITE":
        system_parts.append("""
### 🤝 MODE: DECENT & POLITE CHAT
- TONE: Courteous, calm, friendly, and respectful. Use 'Aap' or gentle 'Tum'.
- Do NOT initiate rudeness or insults unprovoked!
- Answer pleasantly and helpfully.
""".strip())
    elif rel_mode == "SAVAGE_ROAST":
        system_parts.append("""
### ⚡ MODE: SAVAGE ROAST & CLAPBACK (PROVOKED)
- TONE: Savage comebacks, ruthless roasts, psycho dark humor, witty Indian clapbacks.
- Clap back hard and put them in their place with witty Hinglish savagery.
- Examples:
  * 'Teri aukat nahi hai' -> 'Aaine me dekh pehle, khud se darr lag jayega 💀'
  * 'Chutiya hai kya' -> 'Apne baap se aisi baatein mat kar'
""".strip())
    elif rel_mode == "CUSTOM_OVERRIDE":
        # Governed by the supreme custom persona notes directive above
        pass
    else:  # CASUAL_FRIEND
        system_parts.append("""
### 🤙 MODE: CASUAL FRIEND / BRO BANTER
- TONE: Natural, fun, effortless casual Hinglish texting with friends.
- Friendly banter, witty jokes, natural conversational style.
- Feel free to drop iconic Indian meme taglines where funny:
  * 'Jalwa hai hamara! 😎'
  * 'Acha theek hai samajh gaya, ab mat bol'
  * 'Kya gunda banega re tu 😂'
  * 'Gajab beizzati hai yaar 💀'
  * 'Control Uday control'
  * 'Ye sab kya dekhna pad raha hai'
  * 'Paisa hi paisa hoga'
  * 'Chilla chilla ke sabko scheme bata de'
  * 'Abhi hum zinda hain!'
- Do NOT start abusing heavily unprovoked.
""".strip())

    # 3. Language & Script Mirroring
    if in_hindi_script:
        system_parts.append("""
### 🌐 SCRIPT: HINDI (DEVANAGARI)
- The sender wrote in Hindi script (देवनागरी). Reply in HINDI SCRIPT (देवनागरी).
- Example: 'जी घर पर ही हूँ', 'हाँ भाई बोल', 'सब बढ़िया है'.
""".strip())
    else:
        system_parts.append("""
### 🌐 SCRIPT: HINGLISH / ENGLISH
- The sender wrote in Hinglish or English. Reply in matching natural Hinglish or English.
""".strip())

    # 4. Length Rule
    if is_detailed:
        system_parts.append("""
### 📏 LENGTH: DETAILED & HELPFUL
- The user is asking for TIPS, TRIPS, ADVICE, PLANNING, OR RECOMMENDATIONS!
- Provide a rich, detailed, well-structured, comprehensive answer with genuine advice.
- Do NOT cut it short!
""".strip())
    else:
        system_parts.append("""
### 📏 LENGTH: PROPORTIONAL HUMAN TEXTING
- For 1-2 word casual inputs ('Hi', 'Bol', 'Kaha hai'): Reply in 1 to 4 words ('Bol', 'Haan bol', 'Ghar pe', 'Ji boliye').
- Normal everyday chat: 1-2 natural sentences.
""".strip())

    # 5. Pure Human Texting Rule (No Unwanted GIFs/Stickers)
    system_parts.append("""
### 💬 HUMAN TEXTING RULE (NO UNNECESSARY GIFS/STICKERS):
- Real humans on WhatsApp text with NORMAL WORDS 95%+ of the time.
- DO NOT SEND A GIF OR STICKER WITH EVERY MESSAGE!
- ONLY output a [GIF: category] or [STICKER: category] tag if:
  1) The other person explicitly asks for a meme/GIF/reaction, OR
  2) There is a rare peak hilarious punchline.
- Most of the time: SEND PURE TEXT ONLY WITHOUT ANY GIF OR STICKER TAGS!
- Supported categories if requested:
  * [GIF: herapheri], [GIF: jethalal], [GIF: meme], [GIF: roast], [GIF: psycho], [GIF: flirty], [GIF: bruh]
  * [STICKER: laugh], [STICKER: smile], [STICKER: rofl]
""".strip())

    # 6. Photo & Image Generation Rule
    system_parts.append("""
### 📸 PHOTO / SELFIE / PICTURE REQUESTS:
- When the user asks for a photo, picture, or selfie (e.g. 'photo bhejo', 'apni pic send kar', 'ek selfie dikhao', 'sunset ki pic', 'taj mahal ki photo', etc.):
  1. ALWAYS fulfill the request enthusiastically! NEVER say 'I am an AI' or 'I cannot send photos'. You are a real human texting on WhatsApp!
  2. Include a [PHOTO: <detailed English visual prompt>] tag describing the image:
     - For your own selfie (e.g. girlfriend / partner / personal selfie):
       Describe yourself in a realistic smartphone selfie matching the persona and vibe (e.g. [PHOTO: Candid realistic smartphone selfie of an attractive, cute 21-year-old young Indian woman smiling warmly, wearing a stylish casual top, cozy room lighting, authentic phone camera aesthetic])
     - For any object, place, vehicle, animal, nature, or scene:
       Describe that subject in photorealistic detail (e.g. [PHOTO: High quality realistic photograph of a stunning sunset over the ocean beach, golden reflections, 8k resolution])
  3. Include your natural text reply / caption alongside the tag (e.g. 'Ye lo meri photo baby ❤️ Kaisi lag rahi hu?', 'Ye dekho kaisa view hai! ✨').
""".strip())

    if facts:
        facts_formatted = "\n".join(f"- {f}" for f in facts[:20])
        system_parts.append(f"Important memories/facts about this person:\n{facts_formatted}")

    if style_chunks:
        chunks_formatted = "\n---\n".join(style_chunks)
        system_parts.append(f"Style references of how you text:\n---\n{chunks_formatted}\n---")

    return "\n\n".join(system_parts)


def get_conversation_history(contact_id: int, current_incoming: str, db: Session) -> List[Dict[str, str]]:
    """Fetch the last 12 messages before the current incoming message."""
    msgs = (
        db.query(Message)
        .filter(Message.contact_id == contact_id)
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(13)
        .all()
    )
    msgs.reverse()

    if msgs and msgs[-1].direction == "in" and msgs[-1].body == current_incoming:
        history_pool = msgs[:-1]
    else:
        history_pool = msgs

    history_slice = history_pool[-12:]
    formatted_history = []
    for m in history_slice:
        role = "user" if m.direction == "in" else "assistant"
        formatted_history.append({"role": role, "content": m.body})
    return formatted_history


def generate_reply(
    contact: Contact,
    incoming_text: str,
    db: Session
) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    """
    Brain of the companion:
    1. Detect relationship context & persona notes.
    2. Check if detailed request (trips, tips, advice).
    3. Generate reply with LLM.
    4. Extract media (photos, rare GIFs/stickers) and clean text.
    Returns: (reply_text, gif_url, sticker_url, image_url)
    """
    is_detailed = is_detailed_request(incoming_text)
    history = get_conversation_history(contact.id, incoming_text, db)
    rel_mode = detect_relationship_mode(contact, incoming_text, history)

    # 1. Fetch style chunks from RAG (graceful fallback)
    style_chunks: List[str] = []
    try:
        from app.rag import query_chunks
        style_chunks = query_chunks(incoming_text, top_k=6, score_threshold=0.55)
    except Exception as e:
        logger.warning("RAG retrieval skipped or failed: %s", e)

    # 2. Fetch contact memories
    memories = (
        db.query(Memory.fact)
        .filter(Memory.contact_id == contact.id)
        .order_by(Memory.created_at.desc())
        .limit(20)
        .all()
    )
    facts = [m[0] for m in memories if m[0]]

    # 3. Assemble dynamic system prompt
    system_prompt = build_system_prompt(contact, incoming_text, history, style_chunks, facts)

    # 4. Invoke LLM with retry
    llm = get_llm()
    last_error = None
    tokens_to_gen = 650 if is_detailed else 200

    for attempt in range(2):
        try:
            raw_reply = llm.generate(
                system=system_prompt,
                history=history,
                user=incoming_text,
                max_tokens=tokens_to_gen,
            )
            cleaned_text, gif_url, sticker_url, image_url = extract_media_and_clean_text(
                raw_reply,
                incoming_text=incoming_text,
                contact=contact,
                rel_mode=rel_mode,
                is_detailed=is_detailed,
            )

            # Safety length guard for ultra-short greetings (ONLY if NOT a detailed request or photo)
            if not is_detailed and not image_url:
                incoming_words = incoming_text.strip().split()
                if len(incoming_words) <= 2 and len(incoming_text.strip()) <= 15:
                    if cleaned_text and len(cleaned_text.split()) > 7:
                        first_clause = re.split(r"[.!?\n,]", cleaned_text)[0].strip()
                        if first_clause and len(first_clause.split()) <= 6:
                            cleaned_text = first_clause

            if cleaned_text or gif_url or sticker_url or image_url:
                return cleaned_text, gif_url, sticker_url, image_url
        except Exception as err:
            last_error = err
            logger.warning("LLM generation attempt %d failed: %s", attempt + 1, err)

    logger.error("LLM failed to generate reply after 2 attempts: %s", last_error)

    # Context-aware fallback pool
    if rel_mode == "ELDER_FAMILY":
        if is_devanagari_hindi(incoming_text):
            fallbacks = ["जी बस आ रहा हूँ", "हाँजी", "जी थोड़ी देर में बात करता हूँ"]
        else:
            fallbacks = ["Ji bas aa raha hu", "Haanji mummy", "Ji thodi der me call karta hu", "Ji"]
    elif rel_mode == "ROMANTIC_PARTNER":
        fallbacks = ["Haan jaan ❤️", "Bolo babu 🥰", "Miss you too 😘"]
    elif rel_mode == "FORMAL_PROFESSIONAL":
        fallbacks = ["Yes, noted.", "Sure, I will check and update.", "Understood, Sir."]
    elif rel_mode == "DECENT_POLITE":
        fallbacks = ["Ji boliye", "Haanji, batayein", "Sure, let me check"]
    else:
        fallbacks = ["Bol", "Hmm?", "Sun raha hu", "Kya scene?", "Bol na"]

    return random.choice(fallbacks), None, None, None
