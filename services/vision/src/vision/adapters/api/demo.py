"""What Vision refuses in the public demo (ADR 0068).

The demo's AI assistant is the installation default with the rule based fake model
(``fake/recover``), so "AI recover" works without any model provider. Refused: setting, testing
or removing an own provider (a key or base URL would make the server call a third party) and
uploading own pictures.
"""

from mowdeck_service_kit.demo import DemoPolicy, rule

_P = "/api/v1/vision"

DEMO_POLICY = DemoPolicy(
    blocked=(
        rule(
            rf"{_P}/me/provider(/test)?",
            "The demo uses a built-in demo model; your own AI provider works in your own MowDeck.",
        ),
        rule(
            rf"{_P}/mowers/[^/]+/recover/upload",
            "Uploading pictures is not available in the demo.",
        ),
    )
)
