"""Prompt templates and prompt-building helpers. Pure string work -- no I/O."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# Shared instruction block on how to weigh a mixed review, appended into every
# sentiment prompt below (product/service, single/batch). Added after a QA
# pass found 36 reviews rated >=4 stars but tagged "negative" -- almost all a
# single real complaint (a magnetic cover, a promo code) inside an otherwise
# clearly positive review, overriding the overall tone. The rating-mismatch
# guidance runs the other way deliberately: a separate QA pass on rating<=2 +
# "positive" reviews found the *rating*, not the text, was usually the
# unreliable signal (star-widget misclicks, or a rating about an unrelated
# aspect like delivery) -- so this does not simply defer to the star rating,
# it treats it as a secondary cue only.
SENTIMENT_GUIDANCE = """Sentiment reflects the reviewer's OVERALL experience, not just whether any
complaint is present. Weigh the whole review -- opening/closing tone, the
ratio of praise to complaint, and the star rating if given above. A review
that opens with strong praise and mentions one minor issue (e.g. "best lamp
ever, but the cover doesn't fit") is still positive overall -- only mark it
negative when the complaint clearly outweighs the praise, or the reviewer's
own framing is unhappy. If the review's wording is unambiguous but conflicts
sharply with the star rating, trust the review's own wording -- a mismatched
rating is more often a misclick or a rating of something unrelated (e.g.
delivery, a promo code) than a sign the text should be reinterpreted."""

PRODUCT_PROMPT = """Analyze this product review and classify it.

{context}Review: {text}

Respond ONLY with JSON (no markdown, no explanation):
{{
  "sentiment": "positive|negative|neutral",
  "primary_sector": "sewing_needlecraft|art_painting|reading_vision_wellbeing|beauty|medical|jewellery_trade|general|<new_label>",
  "confidence": 0.0-1.0,
  "key_themes": ["theme1", "theme2"]
}}

""" + SENTIMENT_GUIDANCE + """

Sectors describe what the customer uses the product FOR:
- sewing_needlecraft: sewing, embroidery, quilting, stitching, crochet, knitting, and general craft/hobby work (jigsaws, model making, DIY)
- art_painting: painting, drawing, illustration, design, easel or studio work
- reading_vision_wellbeing: reading, plus low vision, eye strain, aging eyesight, or seasonal affective disorder (SAD) / other light-related wellbeing needs
- beauty: makeup, nails, lashes, brows, PMU/microblading, tattoo, salon work
- medical: clinical, healthcare, or medical-care settings
- jewellery_trade: jewellery making, workshop, trade, industrial or ESD-sensitive work
- general: no clear use case stated

If the review clearly states a specific use that doesn't fit any sector above,
invent a short new one instead of forcing it into the closest fit -- lowercase,
snake_case, 1-3 words (e.g. "woodworking", "photography"). Only use "general"
when no particular use is stated at all."""

SERVICE_PROMPT = """Analyze this service review and classify it.

{context}Review: {text}

Respond ONLY with JSON (no markdown, no explanation):
{{
  "sentiment": "positive|negative|neutral",
  "primary_aspect": "shipping_quality|customer_service|product_quality|fulfillment_speed|communication|returns_refunds|warranty|general_experience",
  "confidence": 0.0-1.0,
  "key_themes": ["theme1", "theme2"]
}}

""" + SENTIMENT_GUIDANCE + """

Aspects:
- shipping_quality: delivery condition, packaging, damage in transit
- customer_service: support, responsiveness, helpfulness of staff
- product_quality: durability, build quality, performance
- fulfillment_speed: how fast the order arrived
- communication: order updates, clarity, transparency
- returns_refunds: return process, refund handling
- warranty: warranty coverage, guarantee claims
- general_experience: overall experience with no single dominant aspect"""


PRODUCT_BATCH_PROMPT = """Analyze each numbered product review below and classify it.

""" + SENTIMENT_GUIDANCE + """

Sectors describe what the customer uses the product FOR:
- sewing_needlecraft: sewing, embroidery, quilting, stitching, crochet, knitting, and general craft/hobby work (jigsaws, model making, DIY)
- art_painting: painting, drawing, illustration, design, easel or studio work
- reading_vision_wellbeing: reading, plus low vision, eye strain, aging eyesight, or seasonal affective disorder (SAD) / other light-related wellbeing needs
- beauty: makeup, nails, lashes, brows, PMU/microblading, tattoo, salon work
- medical: clinical, healthcare, or medical-care settings
- jewellery_trade: jewellery making, workshop, trade, industrial or ESD-sensitive work
- general: no clear use case stated

If a review clearly states a specific use that doesn't fit any sector above,
invent a short new one instead of forcing it into the closest fit -- lowercase,
snake_case, 1-3 words. Only use "general" when no particular use is stated at all.

{reviews}

Respond ONLY with JSON. Return exactly {count} objects, one per review, each
echoing its "id". No markdown, no commentary:
{{"results": [{{"id": 1, "sentiment": "positive|negative|neutral", \
"primary_sector": "sewing_needlecraft|art_painting|reading_vision_wellbeing|beauty|medical|jewellery_trade|general|<new_label>", \
"confidence": 0.0-1.0, "key_themes": ["theme1", "theme2"]}}]}}"""

SERVICE_BATCH_PROMPT = """Analyze each numbered service review below and classify it.

""" + SENTIMENT_GUIDANCE + """

Aspects:
- shipping_quality: delivery condition, packaging, damage in transit
- customer_service: support, responsiveness, helpfulness of staff
- product_quality: durability, build quality, performance
- fulfillment_speed: how fast the order arrived
- communication: order updates, clarity, transparency
- returns_refunds: return process, refund handling
- warranty: warranty coverage, guarantee claims
- general_experience: overall experience with no single dominant aspect

{reviews}

Respond ONLY with JSON. Return exactly {count} objects, one per review, each
echoing its "id". No markdown, no commentary:
{{"results": [{{"id": 1, "sentiment": "positive|negative|neutral", \
"primary_aspect": "shipping_quality|customer_service|product_quality|\
fulfillment_speed|communication|returns_refunds|warranty|general_experience", \
"confidence": 0.0-1.0, "key_themes": ["theme1", "theme2"]}}]}}"""


def _build_context(product_name: Optional[str], rating: Optional[int]) -> str:
    """Build the optional context block prepended to a single-review prompt.

    Rating is deliberately a secondary cue, not a hard rule -- see the
    "trust the review's own wording" clause in SENTIMENT_GUIDANCE above --
    since a star rating can itself be the unreliable signal (widget
    misclicks, or a rating about something the review text never mentions).
    """
    lines = []
    if product_name:
        lines.append(f"Product: {product_name}")
    if rating is not None:
        lines.append(f"Star rating: {rating}/5")
    return ("\n".join(lines) + "\n") if lines else ""


def _render_batch(reviews: List[Dict[str, Any]], max_chars: int) -> str:
    """Render reviews as a numbered block for a batched prompt."""
    parts = []
    for i, r in enumerate(reviews, start=1):
        head = f"--- Review {i} ---"
        name = r.get("product_name")
        if r.get("review_type") == "product" and name:
            head += f"\nProduct: {name}"
        rating = r.get("rating")
        if isinstance(rating, int):
            head += f"\nStar rating: {rating}/5"
        parts.append(f"{head}\n{(r.get('text') or '')[:max_chars]}")
    return "\n\n".join(parts)


def _build_batch_prompt(reviews: List[Dict[str, Any]], review_type: str,
                        max_chars: int) -> str:
    template = PRODUCT_BATCH_PROMPT if review_type == "product" else SERVICE_BATCH_PROMPT
    return template.format(reviews=_render_batch(reviews, max_chars), count=len(reviews))
