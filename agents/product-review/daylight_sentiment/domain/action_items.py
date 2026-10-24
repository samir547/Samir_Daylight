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

# A review window with fewer analysed reviews than this is skipped: too few
# to base three items on. It is never widened, so its label stays true.
MIN_WINDOW_REVIEWS = 100

# A group (sector group, aspect, region, source, product...) with fewer
# reviews than this is flagged small_sample in the summary: too few to
# support more than an early signal.
SMALL_SAMPLE_REVIEWS = 30
# The per-product section: at most TOP_PRODUCTS products, each with at least
# MIN_PRODUCT_NEGATIVES negative reviews; names cut to PRODUCT_NAME_CHARS.
TOP_PRODUCTS = 10
MIN_PRODUCT_NEGATIVES = 3
PRODUCT_NAME_CHARS = 80
# The trend section compares the most recent third of a window with the
# earlier two thirds, for the main groups and the TREND_THEMES most frequent
# negative themes.
TREND_THEMES = 10
# A trend row (group or theme) whose negatives -- or, for a theme, negative
# reviews mentioning it -- add up to fewer than this across both parts is
# flagged few_mentions: too few to say whether it is rising or falling.
MIN_TREND_COUNT = 10

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
# An "early signal" item's title must not open with one of these verbs (they
# claim a known cause), but with one of EARLY_SIGNAL_VERBS.
STRONG_ACTION_VERBS = ("fix", "overhaul", "resolve", "eliminate", "stop", "eradicate")
EARLY_SIGNAL_VERBS = ("Monitor", "Investigate", "Review")
# Wording that claims more certainty than review counts can give; rejected
# as whole words, in any case, in BANNED_WORDING_FIELDS.
BANNED_WORDS = ("significant", "significantly", "statistically", "meaningful",
                "not random", "random variation")
BANNED_WORDING_FIELDS = ("title", "description", "rationale", "evidence")
_BANNED = re.compile(r"\b(?:" + "|".join(w.replace(" ", r"\s+") for w in BANNED_WORDS)
                     + r")\b", re.I)
_FIRST_WORD = re.compile(r"[^\W\d_]+")
# Field names and flags from the summary ("small_sample", "few_mentions",
# "negative_share_pct"...) mean nothing to the reader; a lower-case
# snake_case word is rejected in IDENTIFIER_FIELDS.
IDENTIFIER_FIELDS = ("title", "description", "rationale", "evidence")
IDENTIFIER_PLAIN_WORDS = ('"small sample"', '"too few reviews"')
_IDENTIFIER = re.compile(r"(?<!\w)[a-z][a-z0-9]*(?:_[a-z0-9]+)+(?!\w)")

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

# Every number in these fields must be one of the summary's numbers.
NUMBER_CHECKED_FIELDS = ("description", "rationale", "evidence")
_MONTHS = (r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
           r"|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?")
# Dates, years and periods ("the last 12 months") -- not figures to check.
_NOT_FIGURES = re.compile(
    r"\b\d{4}-\d{1,2}(?:-\d{1,2})?\b"
    r"|\b\d{1,2}[/.]\d{1,2}[/.]\d{2,4}\b"
    rf"|\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?(?:{_MONTHS})\b\.?(?:,?\s+\d{{4}})?"
    rf"|\b(?:{_MONTHS})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?\b(?:,?\s+\d{{4}})?"
    r"|\b(?:19|20)\d{2}\b"
    r"|\b\d+\s*(?:months?|years?|weeks?|days?)\b",
    re.I)
# A number standing on its own (not part of a code such as "D35500"), with
# an optional percent sign or "per cent".
_NUMBER = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)(?!\w)"
                     r"(\s*(?:%|per\s?cent\b))?", re.I)


class ActionItemsValidationError(ValueError):
    """The model's reply isn't a complete, well-formed set of items."""


@dataclass
class ReviewWindow:
    """The reviews one generation is based on: the last ``months`` months,
    and the period they cover."""
    months: int
    rows: List[Dict[str, Any]]
    start: Optional[date]
    end: Optional[date]
    undated_excluded: int = 0
    skip_reason: Optional[str] = None
    # Set when the newest review date is after today, so the window was
    # ended today instead; future_dated counts the reviews dated after today.
    capped_from: Optional[date] = None
    future_dated: int = 0

    @property
    def label(self) -> str:
        return window_label(self.months)

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


def window_label(months: int) -> str:
    """The name a review window is saved and shown under ("Last 6 months")."""
    return f"Last {months} month{'' if months == 1 else 's'}"


def months_before(end: date, months: int) -> date:
    """The same day ``months`` calendar months before ``end``, clamped to the
    last day of a shorter month (31 March minus one month is 28/29 Feb)."""
    total = end.year * 12 + (end.month - 1) - months
    year, month = divmod(total, 12)
    month += 1
    return date(year, month, min(end.day, calendar.monthrange(year, month)[1]))


def select_windows(rows: Iterable[Dict[str, Any]], windows_months: Sequence[int],
                   today: Optional[date] = None,
                   min_reviews: int = MIN_WINDOW_REVIEWS) -> List[ReviewWindow]:
    """One window of analysed reviews per entry of ``windows_months``.

    Every window ends on the NEWEST review date in the data, so it doesn't
    shrink while no new files arrive -- or on ``today`` (default: the
    current date) if that is earlier, so a wrongly parsed future date can't
    push the windows forward. Reviews dated after today stay in them. A
    window with fewer than ``min_reviews`` reviews is returned skipped (with
    a skip_reason), never widened, so its label always describes it. Rows
    with no sentiment or no parsed review date are left out.
    """
    today = today or date.today()
    analysed = [r for r in rows if r.get("sentiment")]
    dated = []
    for r in analysed:
        d = _as_date(r.get("review_date_parsed"))
        if d is not None:
            dated.append({**r, "review_date_parsed": d})
    undated = len(analysed) - len(dated)

    if not dated:
        return [ReviewWindow(m, [], None, None, undated_excluded=undated,
                             skip_reason="no analysed reviews with a review date")
                for m in windows_months]

    newest = max(r["review_date_parsed"] for r in dated)
    end = min(newest, today)
    capped = {"capped_from": newest if newest > today else None,
              "future_dated": sum(1 for r in dated if r["review_date_parsed"] > today)}
    windows = []
    for months in windows_months:
        start = months_before(end, months) + timedelta(days=1)
        in_window = [r for r in dated if r["review_date_parsed"] >= start]
        skip_reason = None
        if len(in_window) < min_reviews:
            skip_reason = (f"only {len(in_window)} analysed reviews from {start.isoformat()} "
                           f"to {end.isoformat()} (minimum {min_reviews})")
        windows.append(ReviewWindow(months, in_window, start, end, undated_excluded=undated,
                                    skip_reason=skip_reason, **capped))
    return windows


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


def single_spaced(text: str) -> str:
    """Every run of whitespace, line breaks included, as one space. A line
    break dropped by whatever displays the text would otherwise join the
    words either side of it."""
    return " ".join(text.split())


def shorten(text: str, max_chars: int) -> str:
    """``text`` cut to at most ``max_chars`` characters at a word boundary,
    with "..." after the last whole word. A single word longer than the
    limit is the only thing cut mid-word."""
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars - 3]
    if not text[max_chars - 3].isspace() and " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:-") + "..."


def _clean_excerpt(text: str, max_chars: int) -> str:
    text = single_spaced(text)
    text = _EMAIL.sub("[email removed]", text)
    text = _PHONE.sub("[number removed]", text)
    return shorten(text, max_chars)


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
    return shorten(name, PRODUCT_NAME_CHARS)


def products_most_negative(product_rows: Iterable[Dict[str, Any]],
                           small_sample_reviews: int) -> List[Dict[str, Any]]:
    """The products with the most negative reviews, by resolved_sku (the
    one product key the view resolves for both sources), named by their
    most frequent product_name. Products with fewer than
    MIN_PRODUCT_NEGATIVES negatives are left out; at most TOP_PRODUCTS."""
    tallies: Dict[str, _Tally] = defaultdict(_Tally)
    names: Dict[str, Counter] = defaultdict(Counter)
    regions: Dict[str, Counter] = defaultdict(Counter)
    sources: Dict[str, Counter] = defaultdict(Counter)
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
        sources[sku][r.get("review_source") or "unknown"] += 1
    kept = [(sku, t) for sku, t in tallies.items() if t.negatives >= MIN_PRODUCT_NEGATIVES]
    kept.sort(key=lambda kv: (-kv[1].negatives, -kv[1].reviews, kv[0]))
    out = []
    for sku, t in kept[:TOP_PRODUCTS]:
        entry = {"product": _product_name(names[sku], sku), "sku": sku}
        if regions[sku]:
            entry["region"] = regions[sku].most_common(1)[0][0]
        entry["reviews_by_source"] = dict(sorted(sources[sku].items()))
        out.append({**entry, **t.as_dict(small_sample_reviews)})
    return out


def _by_source(rows: Iterable[Dict[str, Any]], key, label: str,
               small_sample_reviews: int) -> List[Dict[str, Any]]:
    """Reviews per (group, review source), for comparisons within one
    source: [{label: group, "review_source": ..., reviews, negatives, ...}]."""
    tallies: Dict[tuple, _Tally] = defaultdict(_Tally)
    for r in rows:
        tallies[(key(r), r.get("review_source") or "unknown")].add(r.get("sentiment"))
    return [{label: group, "review_source": src, **t.as_dict(small_sample_reviews)}
            for (group, src), t in sorted(tallies.items())]


def recent_part_start(window: ReviewWindow) -> date:
    """First day of the most recent third of the window (its last third of
    days, at least one day). Reviews dated after window.end, if any, count
    as recent."""
    days = (window.end - window.start).days + 1
    return window.end - timedelta(days=max(1, days // 3) - 1)


def _trend_entry(recent: _Tally, earlier: _Tally, small_sample_reviews: int,
                 min_count: int) -> Dict[str, Any]:
    return {"recent": recent.as_dict(small_sample_reviews),
            "earlier": earlier.as_dict(small_sample_reviews),
            "few_mentions": recent.negatives + earlier.negatives < min_count}


def _trend_groups(rows: Iterable[Dict[str, Any]], key, recent_start: date,
                  order: Sequence[Any], small_sample_reviews: int,
                  min_count: int) -> List[tuple]:
    """(group, trend entry) for each group in ``order``: its tally in the
    recent part and in the earlier part."""
    recent: Dict[Any, _Tally] = defaultdict(_Tally)
    earlier: Dict[Any, _Tally] = defaultdict(_Tally)
    for r in rows:
        part = recent if r["review_date_parsed"] >= recent_start else earlier
        part[key(r)].add(r.get("sentiment"))
    return [(group, _trend_entry(recent[group], earlier[group], small_sample_reviews,
                                 min_count))
            for group in order]


def _theme_trend(rows: Sequence[Dict[str, Any]], themes: Sequence[str], recent_start: date,
                 small_sample_reviews: int, min_count: int) -> List[Dict[str, Any]]:
    """For each theme: how many negative reviews mention it in each part,
    as a share of ALL the reviews in that part (so the two parts compare
    whatever their size; never a share of the negative reviews)."""
    totals = {"recent": 0, "earlier": 0}
    counts: Dict[str, Counter] = {"recent": Counter(), "earlier": Counter()}
    for r in rows:
        part = "recent" if r["review_date_parsed"] >= recent_start else "earlier"
        totals[part] += 1
        if r.get("sentiment") == "negative":
            counts[part].update({theme_key(t) for t in parse_themes(r.get("key_themes"))})
    out = []
    for theme in themes:
        entry: Dict[str, Any] = {"theme": theme}
        for part in ("recent", "earlier"):
            c = counts[part][theme_key(theme)]
            entry[part] = {"reviews": totals[part], "negatives_mentioning": c,
                           "share_of_reviews_pct": _pct(c, totals[part]),
                           "small_sample": totals[part] < small_sample_reviews}
        entry["few_mentions"] = (entry["recent"]["negatives_mentioning"]
                                 + entry["earlier"]["negatives_mentioning"]) < min_count
        out.append(entry)
    return out


def build_trend(window: ReviewWindow, summary: Dict[str, Any],
                small_sample_reviews: int,
                min_trend_count: int = MIN_TREND_COUNT) -> Dict[str, Any]:
    """The window's most recent third against its earlier two thirds, for
    the groups ``summary`` already lists (in the same order) and its first
    TREND_THEMES negative themes. A row is flagged few_mentions when its
    count across both parts is below ``min_trend_count``."""
    rows = window.rows
    n, m = small_sample_reviews, min_trend_count
    recent_start = recent_part_start(window)
    product = [r for r in rows if r.get("review_type") == "product"]
    service = [r for r in rows if r.get("review_type") == "service"]
    recent_rows = sum(1 for r in rows if r["review_date_parsed"] >= recent_start)

    def named(pairs):
        return [{"name": g, **e} for g, e in pairs]

    trend: Dict[str, Any] = {
        "recent_part": {"start": recent_start.isoformat(), "end": window.end.isoformat(),
                        "reviews": recent_rows},
        "earlier_part": {"start": window.start.isoformat(),
                         "end": (recent_start - timedelta(days=1)).isoformat(),
                         "reviews": len(rows) - recent_rows},
        "by_review_type_and_source": [
            {"review_type": t, "review_source": s, **e} for (t, s), e in _trend_groups(
                rows, lambda r: (r.get("review_type") or "unknown",
                                 r.get("review_source") or "unknown"), recent_start,
                [(x["review_type"], x["review_source"])
                 for x in summary["by_review_type_and_source"]], n, m)],
        "product_by_sector_group": named(_trend_groups(
            product, lambda r: r.get("sector_group") or "Unknown", recent_start,
            [x["name"] for x in summary["product_by_sector_group"]], n, m)),
        "service_by_aspect": named(_trend_groups(
            service, lambda r: aspect_name(r.get("primary_aspect")), recent_start,
            [x["name"] for x in summary["service_by_aspect"]], n, m)),
    }
    if "product_by_region_and_source" in summary:
        trend["product_by_region_and_source"] = [
            {"region": g, "review_source": s, **e} for (g, s), e in _trend_groups(
                [r for r in product if r.get("product_region")],
                lambda r: (r["product_region"], r.get("review_source") or "unknown"),
                recent_start, [(x["region"], x["review_source"])
                               for x in summary["product_by_region_and_source"]], n, m)]
    trend["top_negative_themes"] = _theme_trend(
        rows, [t["theme"] for t in summary["top_negative_themes"][:TREND_THEMES]],
        recent_start, n, m)
    return trend


def build_summary(window: ReviewWindow, top_themes: int, max_excerpts: int,
                  excerpt_chars: int,
                  small_sample_reviews: int = SMALL_SAMPLE_REVIEWS) -> Dict[str, Any]:
    """Compact, JSON-serialisable summary of the window's reviews -- the only
    thing the model is shown. Every grouped section gives reviews,
    negatives, the negative share and small_sample (fewer than
    ``small_sample_reviews`` reviews); "trend" splits the main groups
    between the window's most recent third and its earlier two thirds."""
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
            "label": window.label,
            "start": window.start.isoformat(),
            "end": window.end.isoformat(),
            "reviews": window.review_count,
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
        "product_by_sector_group_and_source": _by_source(
            product, lambda r: r.get("sector_group") or "Unknown", "sector_group", n),
        "service_by_aspect": _grouped(
            service, lambda r: aspect_name(r.get("primary_aspect")), n),
        "service_by_aspect_and_source": _by_source(
            service, lambda r: aspect_name(r.get("primary_aspect")), "aspect", n),
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
    summary["trend"] = build_trend(window, summary, n)
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


@dataclass
class SummaryNumbers:
    """The numbers a reply may use: every number in the summary (rounded to
    1 decimal place) and, separately, its percentages."""
    values: set
    percentages: set

    def supports(self, value: float, percent: bool) -> bool:
        value = round(value, 1)
        if not percent:
            return value in self.values
        if value in self.percentages:
            return True
        # "16%" for a summary share of 15.6 (round half up, as people do).
        return value == int(value) and any(
            int(p + 0.5) == value for p in self.percentages)


def _numbers_in(text: str):
    """(value, is_percentage, as written) for each figure in ``text``,
    leaving out dates, years and periods."""
    text = _NOT_FIGURES.sub(" ", text)
    for m in _NUMBER.finditer(text):
        yield float(m.group(1).replace(",", "")), bool(m.group(2)), m.group(0).strip()


def summary_numbers(summary: Any) -> SummaryNumbers:
    numbers = SummaryNumbers(set(), set())

    def walk(value: Any, key: str = "") -> None:
        if isinstance(value, bool):
            return
        if isinstance(value, (int, float)):
            numbers.values.add(round(float(value), 1))
            if key.endswith("_pct"):
                numbers.percentages.add(round(float(value), 1))
        elif isinstance(value, str):
            for number, percent, _ in _numbers_in(value):
                numbers.values.add(round(number, 1))
                if percent:
                    numbers.percentages.add(round(number, 1))
        elif isinstance(value, dict):
            for k, v in value.items():
                walk(v, k)
        elif isinstance(value, list):
            for v in value:
                walk(v, key)

    walk(summary)
    return numbers


def unsupported_numbers(text: str, known: SummaryNumbers) -> List[str]:
    """The figures in ``text``, as written, that the summary doesn't give."""
    return [written for value, percent, written in _numbers_in(text)
            if not known.supports(value, percent)]


def banned_wording(text: str) -> List[str]:
    """The BANNED_WORDS in ``text`` (whole words, any case), lower-cased,
    each once, in the order they first appear."""
    found: List[str] = []
    for m in _BANNED.finditer(text):
        word = " ".join(m.group(0).lower().split())
        if word not in found:
            found.append(word)
    return found


def field_identifiers(text: str) -> List[str]:
    """The lower-case snake_case words in ``text`` ("small_sample"), each
    once, in the order they first appear."""
    found: List[str] = []
    for m in _IDENTIFIER.finditer(text):
        if m.group(0) not in found:
            found.append(m.group(0))
    return found


def title_verb(title: str) -> str:
    """The first word of ``title``, lower-cased ("" if it has none)."""
    m = _FIRST_WORD.search(title)
    return m.group(0).lower() if m else ""


def _check_item(i: int, item: Any, canonical: Dict[str, str], small: set,
                threshold: Any, known: SummaryNumbers) -> tuple:
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
        value = single_spaced(value)
        if len(value) > FIELD_MAX_CHARS[name]:
            problems.append(f"item {i} {name} is {len(value)} characters "
                            f"(limit {FIELD_MAX_CHARS[name]})")
            continue
        clean[name] = value
        if name in NUMBER_CHECKED_FIELDS:
            bad = unsupported_numbers(value, known)
            if bad:
                problems.append(f"item {i} {name} has numbers that are not in the summary: "
                                f"{', '.join(bad)}")
        if name in BANNED_WORDING_FIELDS:
            banned = banned_wording(value)
            if banned:
                problems.append(f"item {i} {name} uses wording the summary cannot support: "
                                f"{', '.join(banned)}")
        if name in IDENTIFIER_FIELDS:
            identifiers = field_identifiers(value)
            if identifiers:
                problems.append(f"item {i} {name} copies field names from the data: "
                                f"{', '.join(identifiers)}; write them in plain words such "
                                f"as {' or '.join(IDENTIFIER_PLAIN_WORDS)}")

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
            verb = title_verb(clean.get("title", ""))
            if strength == EARLY_SIGNAL and verb in STRONG_ACTION_VERBS:
                problems.append(
                    f"item {i} is an {EARLY_SIGNAL!r} but its title starts with "
                    f"{verb.capitalize()!r}; start it with "
                    f"{', '.join(EARLY_SIGNAL_VERBS[:-1])} or {EARLY_SIGNAL_VERBS[-1]}")
            area = clean.get("related_area", "")
            if area.casefold() in small and strength != EARLY_SIGNAL:
                problems.append(
                    f"item {i} related_area {area!r} has fewer than {threshold} reviews "
                    f"(small_sample), so evidence_strength must be {EARLY_SIGNAL!r}, "
                    f"not {strength!r}")
    return (None if problems else clean), problems


def validate_action_items(reply: Any, summary: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Check the model's reply against the summary it was given and return
    the items ordered by rank, with text single-spaced (line breaks
    included), related_area in its
    canonical spelling and evidence_strength lower-case. Every number in
    the description, rationale and evidence must be in the summary (dates,
    years and periods aside; a percentage may be rounded to a whole
    number). None of BANNED_WORDS may appear in the title, description,
    rationale or evidence, nor may a snake_case field name such as
    "small_sample", and an "early signal" title may not start with
    one of STRONG_ACTION_VERBS. Anything short of
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
    known = summary_numbers(summary)
    out: List[Dict[str, Any]] = []
    problems: List[str] = []
    for i, item in enumerate(items, start=1):
        clean, found = _check_item(i, item, canonical, small, threshold, known)
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
    lines = [f"{window.label}: top {len(items)} to-do items -- {window.basis()}", ""]
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
