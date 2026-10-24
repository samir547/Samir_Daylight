"""Business to-do items from the stored sentiment results: review-window
selection, the compact summary the model is shown, and validation of the
model's reply. Pure logic -- no I/O; every limit is passed in by the caller
(see ActionItemSettings in config.py).

Everything the model is told comes from build_summary()'s output, and the
reviewer_name column is never read, so no reviewer is identified in it.
"""
from __future__ import annotations

import calendar
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence

ITEM_COUNT = 3
OVERALL_AREA = "overall"

# A group (sector group, aspect, region, source, product...) with fewer
# reviews than this is flagged small_sample in the summary: too few to
# support more than an early signal.
SMALL_SAMPLE_REVIEWS = 30
# The per-product section: at most TOP_PRODUCTS products, each with at least
# MIN_PRODUCT_NEGATIVES negative reviews; names cut to PRODUCT_NAME_CHARS.
TOP_PRODUCTS = 10
MIN_PRODUCT_NEGATIVES = 3
PRODUCT_NAME_CHARS = 80

# Service-review aspect labels (domain/taxonomy.ASPECTS) as the reader sees
# them: in the summary, in related_area and so in Power BI.
ASPECT_NAMES = {
    "shipping_quality": "Shipping quality",
    "customer_service": "Customer service",
    "product_quality": "Product quality",
    "fulfillment_speed": "Delivery speed",
    "communication": "Communication",
    "returns_refunds": "Returns & refunds",
    "warranty": "Warranty",
    "general_experience": "General experience",
}
ITEM_FIELDS = ("title", "description", "rationale", "related_area", "evidence",
               "evidence_strength")
EARLY_SIGNAL = "early signal"
EVIDENCE_STRENGTHS = ("strong", "moderate", EARLY_SIGNAL)

# Caps follow dbo.sentiment_action_items: title NVARCHAR(200), related_area
# NVARCHAR(100), evidence_strength NVARCHAR(20). The NVARCHAR(MAX) text
# fields get a cap that keeps an item readable in a Power BI card.
FIELD_MAX_CHARS = {
    "title": 200,
    "description": 1000,
    "rationale": 1500,
    "related_area": 100,
    "evidence": 1000,
    "evidence_strength": 20,
}

# Free review text can carry contact details; these are blanked in excerpts.
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
_PHONE = re.compile(r"\+?\d[\d ()-]{7,}\d")


class ActionItemsValidationError(ValueError):
    """The model's reply isn't a complete, well-formed set of items."""


@dataclass
class ReviewWindow:
    """The reviews a generation is based on, and the period they cover."""
    rows: List[Dict[str, Any]]
    start: Optional[date]
    end: Optional[date]
    widened: bool = False
    undated_excluded: int = 0
    skip_reason: Optional[str] = None

    @property
    def skipped(self) -> bool:
        return self.skip_reason is not None

    @property
    def review_count(self) -> int:
        return len(self.rows)

    def basis(self) -> str:
        return (f"Based on {self.review_count} reviews from "
                f"{self.start.isoformat()} to {self.end.isoformat()}")


@dataclass
class _Tally:
    reviews: int = 0
    negatives: int = 0
    by_sentiment: Counter = field(default_factory=Counter)

    def add(self, sentiment: str) -> None:
        self.reviews += 1
        self.by_sentiment[sentiment] += 1
        if sentiment == "negative":
            self.negatives += 1

    def as_dict(self, small_sample_reviews: int) -> Dict[str, Any]:
        return {"reviews": self.reviews, "negatives": self.negatives,
                "negative_share_pct": _pct(self.negatives, self.reviews),
                "small_sample": self.reviews < small_sample_reviews}


def _pct(part: int, whole: int) -> float:
    return round(100.0 * part / whole, 1) if whole else 0.0


def _as_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def months_before(end: date, months: int) -> date:
    """The same day ``months`` calendar months before ``end``, clamped to the
    last day of a shorter month (31 March minus one month is 28/29 Feb)."""
    total = end.year * 12 + (end.month - 1) - months
    year, month = divmod(total, 12)
    month += 1
    return date(year, month, min(end.day, calendar.monthrange(year, month)[1]))


def select_window(rows: Iterable[Dict[str, Any]], months: int,
                  min_window_reviews: int, min_total_reviews: int) -> ReviewWindow:
    """Pick the analysed reviews to base the items on.

    The window is the ``months`` months ending on the NEWEST review date in
    the data (not today), so it doesn't shrink while no new files arrive.
    Fewer than ``min_window_reviews`` in it widens to every dated review;
    fewer than ``min_total_reviews`` even then skips generation. Rows with
    no sentiment or no parsed review date are left out.
    """
    analysed = [r for r in rows if r.get("sentiment")]
    dated = []
    for r in analysed:
        d = _as_date(r.get("review_date_parsed"))
        if d is not None:
            dated.append({**r, "review_date_parsed": d})
    undated = len(analysed) - len(dated)

    if not dated:
        return ReviewWindow([], None, None, undated_excluded=undated,
                            skip_reason="no analysed reviews with a review date")

    end = max(r["review_date_parsed"] for r in dated)
    start = months_before(end, months) + timedelta(days=1)
    in_window = [r for r in dated if r["review_date_parsed"] >= start]
    if len(in_window) >= min_window_reviews:
        return ReviewWindow(in_window, start, end, undated_excluded=undated)

    earliest = min(r["review_date_parsed"] for r in dated)
    if len(dated) < min_total_reviews:
        return ReviewWindow(
            dated, earliest, end, widened=True, undated_excluded=undated,
            skip_reason=(f"only {len(dated)} dated, analysed reviews in total "
                         f"(minimum {min_total_reviews})"))
    return ReviewWindow(dated, earliest, end, widened=True, undated_excluded=undated)


def parse_themes(raw: Any) -> List[str]:
    """key_themes as stored (JSON text of a list), parsed defensively:
    malformed text falls back to a comma split; values are lower-cased with
    underscores and repeated spaces collapsed, and de-duplicated per review."""
    if raw is None:
        return []
    items: Any = raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            items = json.loads(text)
        except ValueError:
            items = text.strip("[]").split(",")
    if isinstance(items, str):
        items = [items]
    if not isinstance(items, list):
        return []
    out: List[str] = []
    for item in items:
        if not isinstance(item, str):
            continue
        theme = " ".join(item.replace("_", " ").strip().strip("'\"").lower().split())
        if theme and theme not in out:
            out.append(theme)
    return out


def aspect_name(label: Optional[str]) -> str:
    """Readable name of a service aspect label ("returns_refunds" ->
    "Returns & refunds"); a label outside ASPECT_NAMES is humanised."""
    if not label:
        return "Unknown"
    if label in ASPECT_NAMES:
        return ASPECT_NAMES[label]
    text = " ".join(label.replace("_", " ").split())
    return text[:1].upper() + text[1:].lower()


# Theme words reduced to a shared stem so that "defective product" and
# "product defect" count as one theme. Kept short and explicit on purpose.
_THEME_STEMS = {"defective": "defect", "broken": "break", "broke": "break"}


def _theme_word(word: str) -> str:
    word = _THEME_STEMS.get(word, word)
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def theme_key(theme: str) -> str:
    """Key under which near-identical themes are counted together: the
    words singularised, stemmed (_THEME_STEMS) and put in alphabetical
    order, so plural and word-order variants share one count."""
    return " ".join(sorted(_theme_word(w) for w in theme.split()))


def count_negative_themes(rows: Iterable[Dict[str, Any]]) -> List[tuple]:
    """(theme, negative reviews mentioning it), most frequent first.
    Near-identical themes are merged (theme_key) and each review counts
    once per merged theme, which is shown under its most frequent
    spelling."""
    counts: Counter = Counter()
    spellings: Dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        if r.get("sentiment") != "negative":
            continue
        keys = set()
        for theme in parse_themes(r.get("key_themes")):
            key = theme_key(theme)
            spellings[key][theme] += 1
            keys.add(key)
        counts.update(keys)
    label = {k: min(v.items(), key=lambda kv: (-kv[1], kv[0]))[0]
             for k, v in spellings.items()}
    return sorted(((label[k], c) for k, c in counts.items()), key=lambda tc: (-tc[1], tc[0]))


def _area(row: Dict[str, Any]) -> str:
    if row.get("review_type") == "product":
        return row.get("sector_group") or "Unknown"
    if row.get("review_type") == "service":
        return aspect_name(row.get("primary_aspect"))
    return OVERALL_AREA


def _clean_excerpt(text: str, max_chars: int) -> str:
    text = " ".join(text.split())
    text = _EMAIL.sub("[email removed]", text)
    text = _PHONE.sub("[number removed]", text)
    if len(text) > max_chars:
        text = text[:max_chars - 3].rstrip() + "..."
    return text


def select_excerpts(rows: Sequence[Dict[str, Any]], max_excerpts: int,
                    max_chars: int) -> List[Dict[str, Any]]:
    """Up to ``max_excerpts`` negative-review quotes: the most confident
    negatives, taken round-robin across sector groups / aspects so one area
    can't fill the list. Deterministic (ties broken by uid). English text is
    preferred; no reviewer name or other column identifies the reviewer."""
    negatives = [r for r in rows if r.get("sentiment") == "negative"]
    negatives.sort(key=lambda r: (-(r.get("confidence") or 0.0), str(r.get("uid"))))
    by_area: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    seen = set()
    for r in negatives:
        text = (r.get("review_text_en") or r.get("review_text") or "").strip()
        key = " ".join(text.lower().split())
        # The same complaint posted twice (or on two sites) is quoted once.
        if text and key not in seen:
            seen.add(key)
            by_area[_area(r)].append({**r, "_text": text})
    order = sorted(by_area, key=lambda a: (-(by_area[a][0].get("confidence") or 0.0), a))

    picked: List[Dict[str, Any]] = []
    depth = 0
    while len(picked) < max_excerpts and any(len(by_area[a]) > depth for a in order):
        for area in order:
            if len(picked) >= max_excerpts:
                break
            if len(by_area[area]) > depth:
                picked.append(by_area[area][depth])
        depth += 1

    return [{
        "review_type": r.get("review_type"),
        "source": r.get("review_source"),
        "area": _area(r),
        "rating": r.get("rating"),
        "text": _clean_excerpt(r["_text"], max_chars),
    } for r in picked]


def _grouped(rows: Iterable[Dict[str, Any]], key,
             small_sample_reviews: int) -> List[Dict[str, Any]]:
    tallies: Dict[str, _Tally] = defaultdict(_Tally)
    for r in rows:
        tallies[key(r)].add(r.get("sentiment"))
    ordered = sorted(tallies.items(), key=lambda kv: (-kv[1].negatives, -kv[1].reviews, kv[0]))
    return [{"name": name, **t.as_dict(small_sample_reviews)} for name, t in ordered]


def _product_name(names: Counter, sku: str) -> str:
    if not names:
        return sku
    name = min(names.items(), key=lambda kv: (-kv[1], kv[0]))[0]
    if len(name) > PRODUCT_NAME_CHARS:
        name = name[:PRODUCT_NAME_CHARS - 3].rstrip() + "..."
    return name


def products_most_negative(product_rows: Iterable[Dict[str, Any]],
                           small_sample_reviews: int) -> List[Dict[str, Any]]:
    """The products with the most negative reviews, by resolved_sku (the
    one product key the view resolves for both sources), named by their
    most frequent product_name. Products with fewer than
    MIN_PRODUCT_NEGATIVES negatives are left out; at most TOP_PRODUCTS."""
    tallies: Dict[str, _Tally] = defaultdict(_Tally)
    names: Dict[str, Counter] = defaultdict(Counter)
    regions: Dict[str, Counter] = defaultdict(Counter)
    for r in product_rows:
        sku = (r.get("resolved_sku") or "").strip()
        if not sku:
            continue
        tallies[sku].add(r.get("sentiment"))
        name = " ".join((r.get("product_name") or "").split())
        if name:
            names[sku][name] += 1
        if r.get("product_region"):
            regions[sku][r["product_region"]] += 1
    kept = [(sku, t) for sku, t in tallies.items() if t.negatives >= MIN_PRODUCT_NEGATIVES]
    kept.sort(key=lambda kv: (-kv[1].negatives, -kv[1].reviews, kv[0]))
    out = []
    for sku, t in kept[:TOP_PRODUCTS]:
        entry = {"product": _product_name(names[sku], sku), "sku": sku}
        if regions[sku]:
            entry["region"] = regions[sku].most_common(1)[0][0]
        out.append({**entry, **t.as_dict(small_sample_reviews)})
    return out


def build_summary(window: ReviewWindow, top_themes: int, max_excerpts: int,
                  excerpt_chars: int,
                  small_sample_reviews: int = SMALL_SAMPLE_REVIEWS) -> Dict[str, Any]:
    """Compact, JSON-serialisable summary of the window's reviews -- the only
    thing the model is shown. Every grouped section gives reviews,
    negatives, the negative share and small_sample (fewer than
    ``small_sample_reviews`` reviews)."""
    rows = window.rows
    overall = _Tally()
    for r in rows:
        overall.add(r.get("sentiment"))

    by_type_source: Dict[tuple, _Tally] = defaultdict(_Tally)
    for r in rows:
        by_type_source[(r.get("review_type") or "unknown",
                        r.get("review_source") or "unknown")].add(r.get("sentiment"))

    product = [r for r in rows if r.get("review_type") == "product"]
    service = [r for r in rows if r.get("review_type") == "service"]

    monthly: Dict[str, _Tally] = defaultdict(_Tally)
    for r in rows:
        monthly[r["review_date_parsed"].strftime("%Y-%m")].add(r.get("sentiment"))

    n = small_sample_reviews
    summary: Dict[str, Any] = {
        "window": {
            "start": window.start.isoformat(),
            "end": window.end.isoformat(),
            "reviews": window.review_count,
            "widened_to_all_dates": window.widened,
        },
        "small_sample_threshold_reviews": n,
        "sentiment": {
            "reviews": overall.reviews,
            "positive": overall.by_sentiment["positive"],
            "negative": overall.by_sentiment["negative"],
            "neutral": overall.by_sentiment["neutral"],
            "negative_share_pct": _pct(overall.negatives, overall.reviews),
        },
        "negative_reviews_total": overall.negatives,
        "by_review_type_and_source": [
            {"review_type": t, "review_source": src, **tally.as_dict(n)}
            for (t, src), tally in sorted(by_type_source.items())
        ],
        "product_by_sector_group": _grouped(
            product, lambda r: r.get("sector_group") or "Unknown", n),
        "service_by_aspect": _grouped(
            service, lambda r: aspect_name(r.get("primary_aspect")), n),
    }
    regional = [r for r in product if r.get("product_region")]
    if regional:
        summary["product_by_region"] = _grouped(regional, lambda r: r["product_region"], n)
        by_region_source: Dict[tuple, _Tally] = defaultdict(_Tally)
        for r in regional:
            by_region_source[(r["product_region"],
                              r.get("review_source") or "unknown")].add(r.get("sentiment"))
        summary["product_by_region_and_source"] = [
            {"region": region, "review_source": src, **tally.as_dict(n)}
            for (region, src), tally in sorted(by_region_source.items())
        ]
    summary["products_most_negative"] = products_most_negative(product, n)
    summary["top_negative_themes"] = [
        {"theme": t, "count": c} for t, c in count_negative_themes(rows)[:top_themes]
    ]
    summary["monthly_negative"] = [
        {"month": m, **monthly[m].as_dict(n)} for m in sorted(monthly)
    ]
    summary["negative_excerpts"] = select_excerpts(rows, max_excerpts, excerpt_chars)
    return summary


# The summary sections whose group names a to-do item's related_area may take.
AREA_SECTIONS = ("product_by_sector_group", "service_by_aspect", "product_by_region")


def summary_areas(summary: Dict[str, Any]) -> List[str]:
    """Values a to-do item's related_area may take: "overall" plus every
    sector group, service aspect (readable name) and region named in the
    summary."""
    areas = [OVERALL_AREA]
    for key in AREA_SECTIONS:
        for entry in summary.get(key, []):
            if entry["name"] not in areas:
                areas.append(entry["name"])
    return areas


def small_sample_areas(summary: Dict[str, Any]) -> List[str]:
    """The related_area values whose group is flagged small_sample."""
    return [entry["name"] for key in AREA_SECTIONS for entry in summary.get(key, [])
            if entry.get("small_sample")]


def _check_item(i: int, item: Any, canonical: Dict[str, str], small: set,
                threshold: Any) -> tuple:
    """(clean item or None, problems) for one item of the reply."""
    if not isinstance(item, dict):
        return None, [f"item {i} is not an object"]
    problems: List[str] = []
    rank = item.get("rank")
    if isinstance(rank, bool) or not isinstance(rank, int):
        problems.append(f"item {i} has no whole-number rank")
    clean: Dict[str, Any] = {"rank": rank}
    for name in ITEM_FIELDS:
        value = item.get(name)
        if not isinstance(value, str) or not value.strip():
            problems.append(f"item {i} has a missing or empty {name}")
            continue
        value = value.strip()
        if len(value) > FIELD_MAX_CHARS[name]:
            problems.append(f"item {i} {name} is {len(value)} characters "
                            f"(limit {FIELD_MAX_CHARS[name]})")
            continue
        clean[name] = value

    if "related_area" in clean:
        area = canonical.get(clean["related_area"].casefold())
        if area is None:
            problems.append(f"item {i} related_area {clean['related_area']!r} "
                            f"is not in the summary")
        else:
            clean["related_area"] = area
    if "evidence_strength" in clean:
        strength = " ".join(clean["evidence_strength"].casefold().split())
        if strength not in EVIDENCE_STRENGTHS:
            problems.append(f"item {i} evidence_strength {clean['evidence_strength']!r} "
                            f"is not one of {', '.join(EVIDENCE_STRENGTHS)}")
        else:
            clean["evidence_strength"] = strength
            area = clean.get("related_area", "")
            if area.casefold() in small and strength != EARLY_SIGNAL:
                problems.append(
                    f"item {i} related_area {area!r} has fewer than {threshold} reviews "
                    f"(small_sample), so evidence_strength must be {EARLY_SIGNAL!r}, "
                    f"not {strength!r}")
    return (None if problems else clean), problems


def validate_action_items(reply: Any, summary: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Check the model's reply against the summary it was given and return
    the items ordered by rank, with text stripped, related_area in its
    canonical spelling and evidence_strength lower-case. Anything short of
    exactly ITEM_COUNT complete items raises ActionItemsValidationError,
    whose message lists every problem found: a partial set is never
    returned."""
    if not isinstance(reply, dict) or not isinstance(reply.get("items"), list):
        raise ActionItemsValidationError('reply has no "items" list')
    items = reply["items"]
    if len(items) != ITEM_COUNT:
        raise ActionItemsValidationError(f"expected {ITEM_COUNT} items, got {len(items)}")

    canonical = {a.casefold(): a for a in summary_areas(summary)}
    small = {a.casefold() for a in small_sample_areas(summary)}
    threshold = summary.get("small_sample_threshold_reviews", SMALL_SAMPLE_REVIEWS)
    out: List[Dict[str, Any]] = []
    problems: List[str] = []
    for i, item in enumerate(items, start=1):
        clean, found = _check_item(i, item, canonical, small, threshold)
        problems += found
        if clean is not None:
            out.append(clean)
    if problems:
        raise ActionItemsValidationError("; ".join(problems))

    ranks = sorted(item["rank"] for item in out)
    if ranks != list(range(1, ITEM_COUNT + 1)):
        raise ActionItemsValidationError(
            f"ranks must be 1-{ITEM_COUNT} once each, got {ranks}")
    return sorted(out, key=lambda item: item["rank"])


def format_action_items(items: Sequence[Dict[str, Any]], window: ReviewWindow) -> str:
    """Plain-text rendering for the console (dry runs)."""
    lines = [f"Top {len(items)} to-do items -- {window.basis()}", ""]
    for item in items:
        lines += [
            f"{item['rank']}. {item['title']}  [{item['related_area']}]",
            f"   What to do: {item['description']}",
            f"   Why: {item['rationale']}",
            f"   Evidence: {item['evidence']}",
            f"   Evidence strength: {item['evidence_strength']}",
            "",
        ]
    return "\n".join(lines).rstrip() + "\n"
