"""Builds the provider of a resolved configuration: the fake for ``fake/...``, else LiteLLM."""

from vision.adapters.providers.fake import FakeVisionProvider
from vision.adapters.providers.llm import LiteLlmProvider
from vision.application.ports import StructuredProvider, VisionProvider
from vision.domain.families import FAKE_FAMILY
from vision.domain.models import ProviderConfig

STRUCTURED_MAX_TOKENS = 2000
"""Output limit of a structured answer (ADR 0075): small JSON objects, not essays."""


class DefaultProviderFactory:
    """``ProviderFactory`` and ``StructuredFactory`` of the service."""

    def __init__(self, *, timeout_seconds: float = 90.0, retries: int = 2) -> None:
        self.timeout_seconds = timeout_seconds
        self.retries = retries

    def create(self, config: ProviderConfig) -> VisionProvider:
        """A provider for ``config`` (keys stay inside the config object)."""
        if config.family == FAKE_FAMILY:
            return FakeVisionProvider(config.model)
        return LiteLlmProvider(config, timeout_seconds=self.timeout_seconds, retries=self.retries)

    def create_structured(self, config: ProviderConfig) -> StructuredProvider:
        """A structured provider for ``config`` with a small output limit."""
        if config.family == FAKE_FAMILY:
            return FakeVisionProvider(config.model)
        return LiteLlmProvider(
            config,
            timeout_seconds=self.timeout_seconds,
            retries=self.retries,
            max_tokens=STRUCTURED_MAX_TOKENS,
        )
