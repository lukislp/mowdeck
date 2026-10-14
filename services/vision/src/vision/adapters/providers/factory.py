"""Builds the provider of a resolved configuration: the fake for ``fake/...``, else LiteLLM."""

from vision.adapters.providers.fake import FakeVisionProvider
from vision.adapters.providers.llm import LiteLlmProvider
from vision.application.ports import VisionProvider
from vision.domain.families import FAKE_FAMILY
from vision.domain.models import ProviderConfig


class DefaultProviderFactory:
    """``ProviderFactory`` of the service."""

    def __init__(self, *, timeout_seconds: float = 90.0, retries: int = 2) -> None:
        self.timeout_seconds = timeout_seconds
        self.retries = retries

    def create(self, config: ProviderConfig) -> VisionProvider:
        """A provider for ``config`` (keys stay inside the config object)."""
        if config.family == FAKE_FAMILY:
            return FakeVisionProvider(config.model)
        return LiteLlmProvider(config, timeout_seconds=self.timeout_seconds, retries=self.retries)
