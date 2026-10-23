import logging
from abc import ABC, abstractmethod
from typing import List, Dict, Optional
from google import genai
from google.genai import types
from groq import Groq
from app.config import settings

logger = logging.getLogger(__name__)


class LLM(ABC):
    """Abstract LLM interface for conversational message generation."""

    @abstractmethod
    def generate(self, system: str, history: List[Dict[str, str]], user: str, max_tokens: int = 250) -> str:
        """
        Generate response given system prompt, conversation history, and incoming user message.
        history format: [{"role": "user"|"assistant", "content": "..."}]
        """
        pass


class GeminiLLM(LLM):
    """Google Gemini implementation using official google-genai SDK."""

    def __init__(self, api_key: str, model_name: str = "gemini-2.0-flash"):
        if not api_key:
            raise ValueError("GEMINI_API_KEY is required for GeminiLLM")
        self.client = genai.Client(api_key=api_key)
        self.model_name = model_name

    def generate(self, system: str, history: List[Dict[str, str]], user: str, max_tokens: int = 250) -> str:
        contents = []
        for msg in history:
            role = "user" if msg.get("role") == "user" else "model"
            content_text = msg.get("content", "")
            if content_text:
                contents.append(
                    types.Content(
                        role=role,
                        parts=[types.Part.from_text(text=content_text)],
                    )
                )

        contents.append(
            types.Content(
                role="user",
                parts=[types.Part.from_text(text=user)],
            )
        )

        config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=0.9,
            max_output_tokens=max_tokens,
        )

        models_to_try = [self.model_name]
        for fb in ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-flash-lite-latest"]:
            if fb not in models_to_try:
                models_to_try.append(fb)

        last_err = None
        for m in models_to_try:
            try:
                response = self.client.models.generate_content(
                    model=m,
                    contents=contents,
                    config=config,
                )
                if response and response.text:
                    return response.text.strip()
            except Exception as e:
                logger.warning("Gemini model '%s' failed (%s); trying fallback...", m, e)
                last_err = e

        if last_err:
            raise last_err
        return ""


class GroqLLM(LLM):
    """Groq implementation using groq SDK."""

    def __init__(self, api_key: str, model_name: str = "llama-3.3-70b-versatile"):
        if not api_key:
            raise ValueError("GROQ_API_KEY is required for GroqLLM")
        self.client = Groq(api_key=api_key)
        self.model_name = model_name

    def generate(self, system: str, history: List[Dict[str, str]], user: str, max_tokens: int = 250) -> str:
        messages = [{"role": "system", "content": system}]
        for msg in history:
            role = "user" if msg.get("role") == "user" else "assistant"
            content_text = msg.get("content", "")
            if content_text:
                messages.append({"role": role, "content": content_text})

        messages.append({"role": "user", "content": user})

        response = self.client.chat.completions.create(
            model=self.model_name,
            messages=messages,
            temperature=0.9,
            max_tokens=max_tokens,
        )
        choice = response.choices[0]
        return (choice.message.content or "").strip()


def get_llm(provider: Optional[str] = None) -> LLM:
    """Factory to instantiate LLM based on provider setting."""
    selected_provider = (provider or settings.LLM_PROVIDER).lower()
    if selected_provider == "gemini":
        return GeminiLLM(api_key=settings.GEMINI_API_KEY, model_name=settings.GEMINI_MODEL)
    elif selected_provider == "groq":
        return GroqLLM(api_key=settings.GROQ_API_KEY, model_name=settings.GROQ_MODEL)
    else:
        raise ValueError(f"Unknown LLM provider: '{selected_provider}'. Expected 'gemini' or 'groq'.")
