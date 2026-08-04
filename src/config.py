from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class LLMDefaults:
    # Имена моделей на май 2026 — менять при выходе новых
    OPENAI_CHAT_LIGHT = "gpt-5-mini"
    OPENAI_CHAT_HEAVY = "gpt-5.5"
    OPENAI_EMBED = "text-embedding-3-small"

    GEMINI_CHAT_LIGHT = "gemini-3.5-flash-lite"
    GEMINI_CHAT_HEAVY = "gemini-3.6-flash"
    GEMINI_EMBED = "gemini-embedding-001"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    bot_token: str = Field(..., description="Токен control-бота из @BotFather")
    owner_telegram_id: int = Field(..., description="Telegram user_id единственного владельца")
    encryption_key: str = Field(..., description="Fernet-ключ (base64)")
    database_url: str = Field("sqlite+aiosqlite:///data/app.db")

    # Свой Whisper large-v3 на Modal GPU. Пусто — режим "modal" недоступен,
    # hybrid откатывается на OpenAI (если задан ключ).
    whisper_modal_url: str = Field("", description="URL Modal-эндпоинта whisper")
    whisper_modal_api_key: str = Field("", description="X-API-Key для Modal-эндпоинта")

    google_sheets_id: str = Field("", description="ID Google-таблиці для щоденного чек-іну")
    google_sheets_credentials_path: str = Field(
        "data/google-sheets-sa.json", description="Шлях до service-account JSON"
    )

    @property
    def modal_stt_configured(self) -> bool:
        return bool(self.whisper_modal_url)

    @property
    def sheets_configured(self) -> bool:
        return bool(self.google_sheets_id) and Path(self.google_sheets_credentials_path).exists()

    @property
    def control_bot_id(self) -> int:
        """user_id этого control-бота — это числовой префикс его же токена до ':'.

        Нужен, чтобы зеркало не писало в БД переписку владельца с самим ботом:
        иначе она попадает в общий поисковый корпус, и /search находит собственные
        ответы бота вместо реальных сообщений.
        """
        return int(self.bot_token.split(":", 1)[0])

    @property
    def data_dir(self) -> Path:
        path = PROJECT_ROOT / "data"
        path.mkdir(parents=True, exist_ok=True)
        return path


settings = Settings()
