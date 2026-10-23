"""JSON strict-mode fix: the Claude analyzer's complete_json() must tolerate raw control
characters inside JSON string values (json.loads strict=False).

Motivating failure: some translation responses carry a literal newline inside
the "translation" string value; the default strict decoder raises
"Invalid control character", failing the translation call even though the
payload is perfectly usable. infra.translation.translate_one() calls
analyzer.complete_json() on whichever provider is active.

Clients are stubbed -- no real API calls are made.
"""
from types import SimpleNamespace

from daylight_sentiment.infra.analyzers import ClaudeSentimentAnalyzer

# A JSON document with a raw (unescaped) newline and tab inside a string value.
PAYLOAD_WITH_CONTROL_CHARS = '{"translation": "line one\nline two\tend"}'
EXPECTED = {"translation": "line one\nline two\tend"}


def _claude_analyzer(text: str) -> ClaudeSentimentAnalyzer:
    analyzer = ClaudeSentimentAnalyzer.__new__(ClaudeSentimentAnalyzer)
    analyzer.config = SimpleNamespace(claude_model="stub")
    resp = SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])
    analyzer.client = SimpleNamespace(messages=SimpleNamespace(
        create=lambda **kwargs: resp))
    return analyzer


def test_claude_complete_json_tolerates_control_characters():
    assert _claude_analyzer(PAYLOAD_WITH_CONTROL_CHARS).complete_json("p") == EXPECTED


def test_claude_complete_json_tolerates_control_characters_inside_fences():
    fenced = "```json\n" + PAYLOAD_WITH_CONTROL_CHARS + "\n```"
    assert _claude_analyzer(fenced).complete_json("p") == EXPECTED


def test_ordinary_payloads_still_parse():
    clean = '{"sentiment": "positive", "confidence": 0.9}'
    expected = {"sentiment": "positive", "confidence": 0.9}
    assert _claude_analyzer(clean).complete_json("p") == expected
