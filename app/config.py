import os
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    WA_PHONE_NUMBER_ID: str = Field(default="", description="WhatsApp Cloud API Phone Number ID")
    WA_ACCESS_TOKEN: str = Field(default="", description="WhatsApp Graph API Bearer Token")
    WA_VERIFY_TOKEN: str = Field(default="choose-any-string", description="Verification token for Meta Webhook")
    WA_APP_SECRET: str = Field(default="", description="Meta App Secret for HMAC-SHA256 signature validation")
    GRAPH_VERSION: str = Field(default="v21.0", description="Meta Graph API version")

    OWNER_NAME: str = Field(default="Owner", description="Owner display name for persona generation")
    DATABASE_URL: str = Field(
        default="postgresql+psycopg://postgres:postgres@localhost:5432/wa",
        description="SQLAlchemy database connection URL",
    )

    PINECONE_API_KEY: str = Field(default="", description="Pinecone API Key")
    PINECONE_INDEX: str = Field(default="chat-style", description="Pinecone index name")
    EMBED_DIM: int = Field(default=768, description="Embedding vector dimension")

    LLM_PROVIDER: str = Field(default="gemini", description="LLM provider: gemini or groq")
    GEMINI_API_KEY: str = Field(default="", description="Google Gemini API key")
    GEMINI_MODEL: str = Field(default="gemini-flash-latest", description="Gemini model name")
    GROQ_API_KEY: str = Field(default="", description="Groq API key")
    GROQ_MODEL: str = Field(default="llama-3.3-70b-versatile", description="Groq model name")

    DASHBOARD_TOKEN: str = Field(default="choose-a-long-random-string", description="Bearer token for dashboard API")
    REPLY_DELAY_SECONDS: int = Field(default=3, description="Delay in seconds before sending auto-reply")
    TZ: str = Field(default="Asia/Kolkata", description="Timezone for scheduler")
    AUTO_ENABLE_NEW_CONTACTS: bool = Field(default=True, description="Automatically enable bot for new incoming contacts")
    WHATSAPP_PROVIDER: str = Field(default="bridge", description="WhatsApp provider: bridge (WhatsApp Web) or meta (Meta Cloud API)")
    BRIDGE_URL: str = Field(default="http://127.0.0.1:3001", description="URL for local Baileys bridge")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    def validate_for_live_traffic(self) -> None:
        """Validate required secrets for live WhatsApp bot operation."""
        missing = []
        if self.WHATSAPP_PROVIDER == "meta":
            if not self.WA_PHONE_NUMBER_ID:
                missing.append("WA_PHONE_NUMBER_ID")
            if not self.WA_ACCESS_TOKEN:
                missing.append("WA_ACCESS_TOKEN")
            if not self.WA_APP_SECRET:
                missing.append("WA_APP_SECRET")
        if self.LLM_PROVIDER == "gemini" and not self.GEMINI_API_KEY:
            missing.append("GEMINI_API_KEY")
        elif self.LLM_PROVIDER == "groq" and not self.GROQ_API_KEY:
            missing.append("GROQ_API_KEY")

        if missing:
            raise ValueError(f"Missing required environment variables for live bot operation: {', '.join(missing)}")


settings = Settings()
