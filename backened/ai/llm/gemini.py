import os
import time
import logging

from dotenv import load_dotenv
from google import genai

from ai.llm.base import LLM
from ai.reliability import get_status_code, is_retryable_error, retry_delay

# =======================================================
# Load Environment Variables
# =======================================================

load_dotenv()

logger = logging.getLogger(__name__)

# =======================================================
# Retry Configuration
# =======================================================

MAX_MODEL_ATTEMPTS = 2

# =======================================================
# Gemini LLM Wrapper
# =======================================================


class GeminiLLM(LLM):
    """
    Shared Gemini client with Multi-API-Key Rotation and Fallback Models.

    Features
    --------
    - Multi-Key Pool: Distributes load across multiple GOOGLE_API_KEYS (comma-separated).
    - 429 Quota Failover: Instantly switches to the next available API key if rate-limited.
    - Model Fallbacks: Tries primary model then fallback models.
    - Dynamic retry and exponential backoff for transient failures.
    """

    def __init__(
        self,
        client=None,
        api_key: str | None = None,
        primary_model: str | None = None,
        fallback_models: list[str] | None = None,
        sleep=time.sleep,
    ):
        self.sleep = sleep

        # -------------------------------------------------------
        # Multi-Key Pool Setup
        # Supports comma-separated keys or separate GOOGLE_API_KEY_1, GOOGLE_API_KEY_2, etc.
        # -------------------------------------------------------
        keys_pool = []
        raw_keys = api_key or os.getenv("GOOGLE_API_KEYS") or os.getenv("GOOGLE_API_KEY") or ""
        for k in raw_keys.split(","):
            if k.strip():
                keys_pool.append(k.strip())

        for i in range(1, 51):
            extra_key = os.getenv(f"GOOGLE_API_KEY_{i}")
            if extra_key and extra_key.strip() and extra_key.strip() not in keys_pool:
                keys_pool.append(extra_key.strip())

        self.api_keys = keys_pool

        if client is not None:
            self.clients = [client]
        elif self.api_keys:
            self.clients = [genai.Client(api_key=k) for k in self.api_keys]
        else:
            self.clients = []

        self._key_index = 0

        # -------------------------------------------------------
        # Model Configuration (Best → Weakest)
        # -------------------------------------------------------
        self.primary_model = primary_model or os.getenv(
            "GEMINI_MODEL", "gemini-3.8-flash"
        )
        configured_fallbacks = os.getenv(
            "GEMINI_FALLBACK_MODELS", "gemini-2.5-flash,gemini-flash-latest"
        )
        self.fallback_models = fallback_models or [
            model.strip()
            for model in configured_fallbacks.split(",")
            if model.strip() and model.strip() != self.primary_model
        ]

        logger.info(
            "GeminiLLM initialized with %d API key(s) | Primary: %s | Fallbacks: %s",
            len(self.clients),
            self.primary_model,
            ", ".join(self.fallback_models),
        )

    @property
    def client(self):
        if not self.clients:
            return None
        return self.clients[self._key_index % len(self.clients)]

    def _rotate_key(self):
        """Rotate to the next API key in the pool."""
        if len(self.clients) > 1:
            self._key_index = (self._key_index + 1) % len(self.clients)
            logger.info(
                "Switched to Gemini API key %d of %d.",
                (self._key_index % len(self.clients)) + 1,
                len(self.clients),
            )

    # =======================================================
    # Internal Chat API Call
    # =======================================================

    def _chat_generate(self, model: str, prompt: str) -> str:
        """Send prompt using current active Gemini client."""
        active_client = self.client
        if active_client is None:
            raise RuntimeError("GOOGLE_API_KEY is not configured.")

        chat = active_client.chats.create(model=model)
        response = chat.send_message(prompt)

        if not response.text:
            raise RuntimeError("Gemini returned an empty response.")

        return response.text.strip()

    # =======================================================
    # Retry Wrapper with Multi-Key Failover
    # =======================================================

    def _generate_with_retry(self, model: str, prompt: str) -> str:
        last_error = None

        # Number of attempts scales with available API keys
        total_attempts = max(MAX_MODEL_ATTEMPTS, len(self.clients))

        for attempt in range(1, total_attempts + 1):
            try:
                logger.info(
                    "Using Gemini model %s [Key %d/%d, Attempt %d/%d]",
                    model,
                    (self._key_index % len(self.clients)) + 1,
                    len(self.clients),
                    attempt,
                    total_attempts,
                )
                result = self._chat_generate(model=model, prompt=prompt)
                # Rotate key after every successful call to distribute RPM evenly across keys
                self._rotate_key()
                return result

            except Exception as error:
                last_error = error
                status_code = get_status_code(error)

                # Invalid key or unauthorized
                if status_code in {401, 403}:
                    if len(self.clients) > 1:
                        logger.warning("Key returned %s; rotating to next key immediately.", status_code)
                        self._rotate_key()
                        continue
                    raise RuntimeError("Gemini API key rejected; check API credentials.") from error

                if status_code == 400:
                    raise RuntimeError("Gemini rejected request structure.") from error

                # 429 Quota / Rate Limit: Immediately rotate to next key if available
                if status_code == 429 or is_retryable_error(error):
                    if len(self.clients) > 1:
                        logger.warning(
                            "Gemini rate limit or error encountered (%s). Rotating key...",
                            error,
                        )
                        self._rotate_key()
                        self.sleep(0.5)
                        continue

                if not is_retryable_error(error) or attempt == total_attempts:
                    logger.warning(
                        "Gemini model %s failed on attempt %d: %s",
                        model,
                        attempt,
                        error,
                    )
                    break

                delay = retry_delay(error, attempt)
                logger.warning(
                    "Gemini temporary failure; retrying in %.2fs...",
                    delay,
                )
                self.sleep(delay)

        raise RuntimeError(f"Gemini model {model} failed.") from last_error

    # =======================================================
    # Public Generate Method
    # =======================================================

    def generate(self, prompt: str) -> str:
        """
        Generate text using primary model and fallback models.

        Each model is attempted at most twice; only transient failures are retried.
        """

        models = [self.primary_model] + self.fallback_models

        if self.client is None:
            raise RuntimeError("GOOGLE_API_KEY is not configured.")

        unique_models = list(dict.fromkeys(models))
        last_error = None

        for model in unique_models:
            try:
                return self._generate_with_retry(model, prompt)

            except Exception as e:
                last_error = e
                if (
                    get_status_code(e) in {400, 401, 403}
                    or str(e).startswith("Gemini rejected the request")
                ):
                    raise
                logger.warning(
                    "Model %s failed. Trying next fallback model...",
                    model,
                )

        raise RuntimeError(
            "All configured Gemini models failed; check model availability and API quota."
        ) from last_error
