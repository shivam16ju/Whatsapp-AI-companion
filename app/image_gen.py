import os
import re
import time
import random
import hashlib
import logging
import urllib.parse
from typing import Optional, Tuple
import httpx

from app.models import Contact

logger = logging.getLogger(__name__)

MEDIA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "media", "generated"))


def is_photo_request(text: str) -> bool:
    """
    Check if the user is asking for a photo, picture, selfie, or visual image.
    Supports English, Hindi, and Hinglish phrasing.
    """
    if not text:
        return False
    t = text.lower().strip()

    # Direct photo words
    photo_words = [
        "photo", "foto", "pic", "pics", "picture", "selfie", "tasveer",
        "image", "phto", "snap"
    ]
    # Intent request words
    request_words = [
        "bhejo", "bhej", "send", "dikhao", "dikha", "do", "karo", "kr",
        "post", "share", "maang", "de na", "bhej na", "show", "give", "chahiye",
        "ki photo", "ka photo", "ke photo", "ki pic", "ka pic", "of"
    ]

    has_photo = any(w in t for w in photo_words)
    has_request = any(r in t for r in request_words)

    # Check for negative intent (e.g. "photo mat bhejo", "don't send photo")
    if any(neg in t for neg in ["mat bhejo", "nahi chahiye", "don't send", "dont send", "mat dikhao"]):
        return False

    # Combinations like "photo bhejo", "pic send karo", "taj mahal ki photo"
    if has_photo and has_request:
        return True

    # Standalone requests like "apni photo", "apni pic", "ek selfie", "teri photo", "apna photo"
    standalone_patterns = [
        r"(?:apni|teri|tumhari|ek|apna)\s+(?:photo|pic|selfie|picture|tasveer)",
        r"(?:photo|pic|selfie|tasveer)\s+(?:please|plz|yr|yaar|na|bhi)",
        r"^(?:photo|pic|selfie|pics)$",
        r"(?:ki|ka|ke)\s+(?:photo|pic|selfie|tasveer|picture)",
        r"(?:photo|pic|picture|tasveer)\s+(?:of|chahiye|de)",
        r"send\s+(?:me\s+)?(?:a\s+)?(?:photo|pic|picture|selfie)",
        r"show\s+(?:me\s+)?(?:your\s+)?(?:photo|pic|face|picture|selfie)",
    ]
    for pattern in standalone_patterns:
        if re.search(pattern, t):
            return True

    return False


def build_photo_prompt(incoming_text: str, contact: Contact, rel_mode: str) -> str:
    """
    Build a realistic, aesthetic English image generation prompt
    based on the user's text, relationship mode, and custom persona notes.
    """
    t_lower = incoming_text.lower()
    persona_notes = (contact.persona_notes or "").lower()

    # 1. Check if user is asking for "selfie" or "apni photo" / "tumhari photo"
    is_asking_for_self = any(
        k in t_lower
        for k in [
            "apni", "teri", "tumhari", "apna", "selfie", "face", "look", "kaha ho",
            "kya kar rahi", "kya kar rahe", "kaisi lag", "kaisa lag", "apne"
        ]
    ) or ("photo" in t_lower and not any(obj in t_lower for obj in ["car", "bike", "dog", "cat", "nature", "taj mahal", "beach", "sunset", "food", "pizza", "flower"]))

    # Girlfriend / Female romantic partner persona
    is_girlfriend = (
        rel_mode == "ROMANTIC_PARTNER" or
        any(k in persona_notes for k in ["girlfriend", "gf", "ladki", "girl", "female", "she", "her", "jaan", "babu", "wife"])
    )

    if is_asking_for_self:
        if is_girlfriend:
            # Check context / mood from text
            if "gym" in t_lower:
                setting = "at a modern fitness gym wearing stylish athletic sportswear, post-workout glow"
            elif any(w in t_lower for w in ["bed", "so rahi", "sone", "raat", "night"]):
                setting = "in a cozy bedroom with soft warm ambient lighting, wearing comfy casual nightwear"
            elif any(w in t_lower for w in ["college", "office", "work", "bahar", "outside"]):
                setting = "outdoors at a trendy urban cafe terrace, natural bright daylight, stylish casual chic outfit"
            elif any(w in t_lower for w in ["saree", "traditional", "ethnic", "suit"]):
                setting = "wearing a gorgeous modern elegant traditional Indian ethnic dress, graceful smile"
            else:
                setting = "in a modern cozy room with soft natural window lighting, wearing a fashionable casual top"

            return (
                f"A candid realistic smartphone selfie of an attractive, cute 21-year-old young Indian woman smiling warmly, "
                f"{setting}, authentic mobile front camera aesthetic, detailed facial features, photorealistic, 8k resolution, natural skin texture"
            )
        else:
            # Male owner selfie
            return (
                "A candid realistic smartphone selfie of a stylish 22-year-old young Indian guy with modern hairstyle, "
                "smiling naturally at the phone camera, casual everyday outfit, warm indoor room lighting, authentic amateur mobile photo, photorealistic"
            )

    # 2. Specific object / scene requested by user
    # Clean text to isolate the subject
    clean_subj = incoming_text
    for remove_word in ["photo", "pic", "pics", "bhejo", "bhej", "send", "karo", "kr", "dikhao", "dikha", "ki", "ka", "ke", "ek", "please", "plz", "tasveer", "de"]:
        clean_subj = re.sub(rf"\b{remove_word}\b", "", clean_subj, flags=re.IGNORECASE)
    clean_subj = clean_subj.strip(" ?:!.,\"'")

    if not clean_subj or len(clean_subj) < 2:
        clean_subj = "beautiful scenic view with stunning lighting"

    return (
        f"A breathtaking high-quality realistic photograph of {clean_subj}, "
        "shot on professional 35mm lens, natural cinematic lighting, highly detailed, photorealistic, 8k resolution"
    )


def generate_image(prompt: str) -> Optional[str]:
    """
    Generate an image from prompt using Pollinations AI.
    Saves the image locally in media/generated/ and returns the absolute file path.
    """
    try:
        os.makedirs(MEDIA_DIR, exist_ok=True)
        # Create unique file name from prompt hash and timestamp
        prompt_hash = hashlib.md5(f"{prompt}_{time.time()}_{random.randint(1000, 9999)}".encode("utf-8")).hexdigest()[:12]
        file_path = os.path.join(MEDIA_DIR, f"img_{prompt_hash}.jpg")

        encoded_prompt = urllib.parse.quote(prompt[:250])
        seed = random.randint(10000, 999999)

        # Candidate URLs to try
        urls = [
            f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=800&height=800&nologo=true&seed={seed}",
            f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=768&height=768&nologo=true",
            f"https://image.pollinations.ai/prompt/{encoded_prompt}?nologo=true&model=turbo",
        ]

        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Accept": "image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        }

        with httpx.Client(timeout=25.0, headers=headers) as client:
            for url in urls:
                try:
                    resp = client.get(url)
                    if resp.status_code == 200 and resp.content and len(resp.content) > 3000:
                        with open(file_path, "wb") as f:
                            f.write(resp.content)
                        logger.info("Successfully generated image (%d bytes) for prompt: %s -> %s", len(resp.content), prompt[:50], file_path)
                        return file_path
                except Exception as req_err:
                    logger.warning("Pollinations request failed on url %s: %s", url[:60], req_err)
                    continue

        logger.error("Failed to generate image from all Pollinations endpoints for prompt: %s", prompt)
        return None

    except Exception as e:
        logger.exception("Unexpected error in generate_image: %s", e)
        return None
