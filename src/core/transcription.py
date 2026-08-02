import asyncio
import gc
import logging
import time
from pathlib import Path

from src.config import settings
from src.db.repo import cache_transcript, get_cached_transcript
from src.db.session import get_session


logger = logging.getLogger(__name__)


# Модель Whisper держит ~150 МБ RSS. Голосовые приходят редко, поэтому после
# простоя выгружаем её и отдаём память — следующий раз загрузится за ~2 с из кэша.
MODEL_IDLE_TIMEOUT_SECONDS = 300


class TranscriptionService:
    """Локальный faster-whisper / OpenAI Whisper API / hybrid (local с fallback в API)."""

    def __init__(self, model_size: str = "small") -> None:
        self._model_size = model_size
        self._model = None
        self._device: str | None = None
        self._lock = asyncio.Lock()
        self._last_used = 0.0
        self._unload_task: asyncio.Task | None = None
        self._busy = 0
        # Один раз убедились, что CUDA не работает (нет cuBLAS) — больше не пробуем:
        # иначе каждая перезагрузка после простоя снова тратит время на неудачную попытку.
        self._cuda_broken = False

    async def _unload_after_idle(self) -> None:
        """Освобождает модель, если ею давно не пользовались."""
        try:
            while True:
                await asyncio.sleep(30)
                if self._model is None:
                    return
                # Пока идёт распознавание, модель забирать нельзя.
                if self._busy:
                    continue
                idle = time.monotonic() - self._last_used
                if idle < MODEL_IDLE_TIMEOUT_SECONDS:
                    continue
                async with self._lock:
                    if self._model is None or self._busy:
                        continue
                    self._model = None
                    self._device = None
                gc.collect()
                logger.info("Whisper model unloaded after %.0fs idle", idle)
                return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("whisper idle-unload watcher failed")

    def _touch(self) -> None:
        self._last_used = time.monotonic()
        if self._unload_task is None or self._unload_task.done():
            try:
                self._unload_task = asyncio.get_running_loop().create_task(
                    self._unload_after_idle(), name="whisper-idle-unload",
                )
            except RuntimeError:
                pass  # вне event loop — просто не выгружаем

    async def _load_model(self, device: str) -> object:
        from faster_whisper import WhisperModel  # тяжёлый импорт держим ленивым

        def _load() -> object:
            # float16 — на GPU, int8 — экономный режим для CPU
            compute_type = "float16" if device == "cuda" else "int8"
            return WhisperModel(self._model_size, device=device, compute_type=compute_type)

        return await asyncio.to_thread(_load)

    async def _ensure_local_model(self) -> object:
        if self._model is not None:
            return self._model
        async with self._lock:
            if self._model is None:
                import ctranslate2

                device = "cpu"
                if not self._cuda_broken:
                    try:
                        if ctranslate2.get_cuda_device_count() > 0:
                            device = "cuda"
                    except Exception:
                        logger.debug("CUDA probe failed", exc_info=True)

                try:
                    self._model = await self._load_model(device)
                    self._device = device
                except Exception:
                    # Не собирается на GPU (нет cuBLAS/cuDNN) — работаем на CPU.
                    logger.warning("Failed to load Whisper on %s, using CPU", device, exc_info=True)
                    self._model = await self._load_model("cpu")
                    self._device = "cpu"
                logger.info("Whisper model %s loaded on %s", self._model_size, self._device)
        return self._model

    async def _transcribe_local(self, path: Path, language: str | None) -> str:
        model = await self._ensure_local_model()
        self._touch()

        def _run(m: object) -> str:
            segments, _info = m.transcribe(str(path), language=language)
            return " ".join(seg.text.strip() for seg in segments).strip()

        self._busy += 1
        try:
            try:
                return await asyncio.to_thread(_run, model)
            except Exception:
                # Библиотеки CUDA часто отваливаются не при загрузке, а при первом вызове.
                if self._device != "cuda":
                    raise
                logger.warning("CUDA transcription failed, reloading on CPU", exc_info=True)
                self._cuda_broken = True
                # Держим модель в локальной переменной: idle-watcher может обнулить
                # self._model в любой момент, и повторный прогон упал бы на None.
                cpu_model = await self._load_model("cpu")
                async with self._lock:
                    self._model = cpu_model
                    self._device = "cpu"
                return await asyncio.to_thread(_run, cpu_model)
        finally:
            self._busy -= 1
            self._touch()  # отсчёт простоя — от конца работы, а не от начала

    async def _transcribe_modal(self, path: Path) -> str:
        """Свой faster-whisper large-v3 на Modal GPU.

        Эндпоинт делался под караоке-тайминги, поэтому отдаёт только слова с
        таймкодами, без готового текста — склеиваем сами.
        """
        import httpx

        url = settings.whisper_modal_url
        if not url:
            raise ValueError("WHISPER_MODAL_URL не задан")

        headers = {}
        if settings.whisper_modal_api_key:
            headers["X-API-Key"] = settings.whisper_modal_api_key

        # Холодный старт поднимает T4 и грузит large-v3 — таймаут щедрый.
        async with httpx.AsyncClient(timeout=600) as client:
            with path.open("rb") as f:
                resp = await client.post(
                    url, headers=headers, files={"file": (path.name, f, "application/octet-stream")}
                )
        resp.raise_for_status()
        payload = resp.json()
        words = [str(w.get("text", "")).strip() for w in payload.get("words", [])]
        return " ".join(w for w in words if w).strip()

    async def _transcribe_api(self, path: Path, openai_key: str, language: str | None) -> str:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=openai_key)
        with path.open("rb") as f:
            resp = await client.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                language=language,
            )
        return resp.text

    async def transcribe(
        self,
        path: Path,
        *,
        file_id: str | None = None,
        mode: str = "hybrid",
        openai_key: str | None = None,
        language: str | None = None,
    ) -> str:
        # file_id используется как ключ кэша — обычно telegram media file_unique_id
        if file_id:
            async with get_session() as session:
                cached = await get_cached_transcript(session, file_id)
                if cached:
                    return cached

        text = ""
        if mode == "api":
            if not openai_key:
                raise ValueError("OpenAI API key required for transcription mode='api'")
            text = await self._transcribe_api(path, openai_key, language)
        elif mode == "modal":
            text = await self._transcribe_modal(path)
        elif mode == "local":
            text = await self._transcribe_local(path, language)
        else:  # hybrid
            try:
                text = await self._transcribe_local(path, language)
            except Exception:
                # Свой Modal предпочтительнее OpenAI: аудио не уходит третьей стороне.
                if settings.modal_stt_configured:
                    logger.exception("Local transcription failed, falling back to Modal")
                    text = await self._transcribe_modal(path)
                elif openai_key:
                    logger.exception("Local transcription failed, falling back to OpenAI")
                    text = await self._transcribe_api(path, openai_key, language)
                else:
                    raise

        if file_id and text:
            async with get_session() as session:
                await cache_transcript(session, file_id, text)
        return text


transcription_service = TranscriptionService()
