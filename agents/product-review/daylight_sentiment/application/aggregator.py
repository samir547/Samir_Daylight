"""Aggregation of per-review analyses into per-source and cross-source stats."""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List


class AnalysisAggregator:
    @staticmethod
    def aggregate_by_source(results: List[Dict[str, Any]]) -> Dict[str, Any]:
        by_source: Dict[str, Any] = defaultdict(lambda: {
            "product": {"reviews": [], "aggregated": {}},
            "service": {"reviews": [], "aggregated": {}},
        })
        for result in results:
            if result.get("analysis") is None:
                continue
            source = result.get("review_source")
            review_type = result.get("review_type")
            if source is None or review_type not in ("product", "service"):
                continue
            by_source[source][review_type]["reviews"].append(result)

        for _source, types in by_source.items():
            for review_type, data in types.items():
                if data["reviews"]:
                    data["aggregated"] = AnalysisAggregator._aggregate_reviews(
                        data["reviews"], review_type)
        return dict(by_source)

    @staticmethod
    def _aggregate_reviews(reviews: List[Dict[str, Any]], review_type: str) -> Dict[str, Any]:
        analyses = [r["analysis"] for r in reviews if r.get("analysis")]
        ratings = [r["rating"] for r in reviews if isinstance(r.get("rating"), int)]
        if not analyses:
            return {}

        sentiment_counts: Dict[str, int] = defaultdict(int)
        for a in analyses:
            sentiment_counts[a.get("sentiment", "neutral")] += 1
        sentiment_dist = {k: v / len(analyses) for k, v in sentiment_counts.items()}

        key = "primary_sector" if review_type == "product" else "primary_aspect"
        cls_counts: Dict[str, int] = defaultdict(int)
        for a in analyses:
            cls_counts[a.get(key, "general")] += 1
        cls_dist = {k: v / len(analyses) for k, v in cls_counts.items()}

        theme_counts: Dict[str, int] = defaultdict(int)
        for a in analyses:
            for t in a.get("key_themes", []):
                theme_counts[t] += 1

        return {
            "total_reviews": len(reviews),
            "avg_rating": round(sum(ratings) / len(ratings), 3) if ratings else None,
            "rating_distribution": {
                str(s): ratings.count(s) for s in sorted(set(ratings))} if ratings else {},
            "sentiment_counts": dict(sentiment_counts),
            "sentiment_distribution": {k: round(v, 4) for k, v in sentiment_dist.items()},
            "classification_counts": dict(cls_counts),
            "classification_distribution": {k: round(v, 4) for k, v in cls_dist.items()},
            "avg_confidence": round(
                sum(a.get("confidence", 0) for a in analyses) / len(analyses), 4),
            "positive_pct": round(sentiment_dist.get("positive", 0) * 100, 2),
            "negative_pct": round(sentiment_dist.get("negative", 0) * 100, 2),
            "neutral_pct": round(sentiment_dist.get("neutral", 0) * 100, 2),
            "top_themes": [
                {"theme": t, "count": c}
                for t, c in sorted(theme_counts.items(), key=lambda x: -x[1])[:15]
            ],
        }

    @staticmethod
    def cross_source_comparison(by_source: Dict[str, Any]) -> Dict[str, Any]:
        comparison: Dict[str, Any] = {}

        tp = by_source.get("trustpilot", {}).get("product", {}).get("aggregated") or {}
        amz = by_source.get("amazon", {}).get("product", {}).get("aggregated") or {}
        if tp and amz:
            tp_sec = tp.get("classification_distribution", {})
            amz_sec = amz.get("classification_distribution", {})
            comparison["product_comparison"] = {
                "trustpilot": tp,
                "amazon": amz,
                "difference": {
                    "sentiment_positive_diff": round(
                        amz.get("positive_pct", 0) - tp.get("positive_pct", 0), 2),
                    "sentiment_negative_diff": round(
                        amz.get("negative_pct", 0) - tp.get("negative_pct", 0), 2),
                    "avg_rating_diff": round(
                        (amz.get("avg_rating") or 0) - (tp.get("avg_rating") or 0), 3),
                    "avg_confidence_diff": round(
                        amz.get("avg_confidence", 0) - tp.get("avg_confidence", 0), 4),
                    "sector_share_diff_pct": {
                        s: round((amz_sec.get(s, 0) - tp_sec.get(s, 0)) * 100, 2)
                        for s in sorted(set(tp_sec) | set(amz_sec))
                    },
                },
            }

        # Product vs service within Trustpilot -- the only source carrying both.
        tp_svc = by_source.get("trustpilot", {}).get("service", {}).get("aggregated") or {}
        if tp and tp_svc:
            comparison["trustpilot_product_vs_service"] = {
                "product_positive_pct": tp.get("positive_pct"),
                "service_positive_pct": tp_svc.get("positive_pct"),
                "positive_pct_diff": round(
                    tp_svc.get("positive_pct", 0) - tp.get("positive_pct", 0), 2),
                "product_avg_rating": tp.get("avg_rating"),
                "service_avg_rating": tp_svc.get("avg_rating"),
            }
        return comparison
