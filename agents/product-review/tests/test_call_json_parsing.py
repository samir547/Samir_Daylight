"""JSON strict-mode fix: both analyzers' _call() must tolerate raw control
characters inside JSON string values (json.loads strict=False).

Motivating failure: some translation responses carry a literal newline inside
the "translation" string value; the default strict decoder raises
"Invalid control character", failing the translation call even though the
payload is perfectly usable. infra.translation.translate_one() calls
analyzer._call() directly on whichever provider is active, so both parse
sites get the same treatment.

Clients are stubbed -- no real API calls are made.
"""
from types import SimpleNamespace

from daylight_sentiment.infra.analyzers import (
    ClaudeSentimentAnalyzer,
    GroqSentimentAnalyzer,
    TokenRateLimiter,
)

# A JSON document with a raw (unescaped) newline and tab inside a string value.
PAYLOAD_WITH_CONTROL_CHARS = '{"translation": "line one\nline two\tend"}'
EXPECTED = {"translation": "line one\nline two\tend"}


def _groq_analyzer(content: str) -> GroqSentimentAnalyzer:
    analyzer = GroqSentimentAnalyzer.__new__(GroqSentimentAnalyzer)
    analyzer.config = SimpleNamespace(
        groq_model="stub", groq_reasoning_effort="low", max_retries=1,
        max_tpd_wait=1.0, max_tpd_waits=0)
    analyzer.limiter = TokenRateLimiter(10_000_000)
    resp = SimpleNamespace(
        usage=SimpleNamespace(total_tokens=100),
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
    )
    analyzer.client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=lambda **kwargs: resp)))
    return analyzer


def _claude_analyzer(text: str) -> ClaudeSentimentAnalyzer:
    analyzer = ClaudeSentimentAnalyzer.__new__(ClaudeSentimentAnalyzer)
    analyzer.config = SimpleNamespace(claude_model="stub")
    resp = SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])
    analyzer.client = SimpleNamespace(messages=SimpleNamespace(
        create=lambda **kwargs: resp))
    return analyzer


def test_groq_call_tolerates_control_characters():
    assert _groq_analyzer(PAYLOAD_WITH_CONTROL_CHARS)._call("p") == EXPECTED


def test_claude_call_tolerates_control_characters():
    assert _claude_analyzer(PAYLOAD_WITH_CONTROL_CHARS)._call("p") == EXPECTED


def test_claude_call_tolerates_control_characters_inside_fences():
    fenced = "```json\n" + PAYLOAD_WITH_CONTROL_CHARS + "\n```"
    assert _claude_analyzer(fenced)._call("p") == EXPECTED


def test_ordinary_payloads_still_parse():
    clean = '{"sentiment": "positive", "confidence": 0.9}'
    expected = {"sentiment": "positive", "confidence": 0.9}
    assert _groq_analyzer(clean)._call("p") == expected
    assert _claude_analyzer(clean)._call("p") == expected
