"""Allowed classification vocabularies.

ASPECTS (service reviews) is still a closed set -- anything the model invents
outside it is coerced to the catch-all. SECTORS (product reviews) is a *seed*
list, not a hard cap: it's Samir's evidence-based end-user segmentation
(Daylight_end_user_segments_evidence.xlsx, Aug 2026 -- a keyword audit over
283 of 872 Trustpilot product reviews that stated a use). _normalise() lets
the model propose a new sector label when a review clearly states a use that
doesn't fit any of these, and only falls back to "general" when no specific
use is stated at all -- see _sanitise_sector_label() in normalise.py.
"""

SECTORS = [
    # sewing, embroidery, quilting, stitching, crochet, knitting, PLUS general
    # craft/hobby (jigsaws, model making) -- merged per Samir's evidence: the
    # "craft" trigger word turned out to be the same customer base as
    # "sew"/"embroider", not a separate DIY population.
    "sewing_needlecraft",
    # painting, drawing, illustration, design, easel/studio work
    "art_painting",
    # reading PLUS low vision, eye strain, aging eyesight, SAD (seasonal
    # affective disorder) -- broadened per Samir's evidence: 22 of 41 reviews
    # in this segment never say "read" at all, only vision/wellbeing language.
    "reading_vision_wellbeing",
    # makeup, nails, lashes, brows, PMU/microblading, tattoo, salon
    "beauty",
    # clinical / healthcare / medical-care settings
    "medical",
    # jewellery making, workshop, trade, industrial, ESD-sensitive work
    "jewellery_trade",
    # no clear use case stated
    "general",
]
ASPECTS = [
    "shipping_quality", "customer_service", "product_quality", "fulfillment_speed",
    "communication", "returns_refunds", "warranty", "general_experience",
]
SENTIMENTS = ["positive", "negative", "neutral"]
