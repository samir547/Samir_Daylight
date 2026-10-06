"""Assembly of the summary JSON output."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List

from daylight_sentiment.config import Config


class OutputGenerator:
    @staticmethod
    def generate(results: List[Dict[str, Any]], by_source: Dict[str, Any],
                 comparison: Dict[str, Any], config: Config) -> Dict[str, Any]:
        analysed = [r for r in results if r.get("analysis") is not None]
        failed = [r for r in results if r.get("error")]

        output: Dict[str, Any] = {
            "metadata": {
                "timestamp": datetime.now().isoformat(),
                "total_reviews": len(results),
                "llm_provider": config.llm_provider.value,
                "llm_model": config.model_name,
            },
            "summary": {
                "total_reviews_analyzed": len(analysed),
                "failed_reviews": len(failed),
                "failure_reasons": OutputGenerator._failure_reasons(failed),
                "overall_sentiment": OutputGenerator._overall(analysed),
            },
            "by_source": {},
            "cross_source_analysis": comparison,
        }

        for source, data in by_source.items():
            entry: Dict[str, Any] = {}
            prod, svc = data["product"], data["service"]
            entry["product_reviews"] = {
                "total": len(prod["reviews"]),
                "aggregated": prod["aggregated"],
                "top_sectors": OutputGenerator._top(
                    prod["aggregated"].get("classification_distribution", {}), 7),
            } if prod["reviews"] else None
            entry["service_reviews"] = {
                "total": len(svc["reviews"]),
                "aggregated": svc["aggregated"],
                "top_aspects": OutputGenerator._top(
                    svc["aggregated"].get("classification_distribution", {}), 8),
            } if svc["reviews"] else None
            output["by_source"][source] = entry

        return output

    @staticmethod
    def _overall(analysed: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not analysed:
            return {}
        counts: Dict[str, int] = defaultdict(int)
        for r in analysed:
            counts[r["analysis"].get("sentiment", "neutral")] += 1
        n = len(analysed)
        return {
            "counts": dict(counts),
            "positive_pct": round(counts["positive"] / n * 100, 2),
            "negative_pct": round(counts["negative"] / n * 100, 2),
            "neutral_pct": round(counts["neutral"] / n * 100, 2),
        }

    @staticmethod
    def _failure_reasons(failed: List[Dict[str, Any]]) -> Dict[str, int]:
        reasons: Dict[str, int] = defaultdict(int)
        for r in failed:
            reasons[str(r.get("error"))[:120]] += 1
        return dict(sorted(reasons.items(), key=lambda x: -x[1]))

    @staticmethod
    def _top(distribution: Dict[str, float], n: int) -> List[Dict[str, Any]]:
        return [
            {"name": k, "percentage": round(v * 100, 2)}
            for k, v in sorted(distribution.items(), key=lambda x: x[1], reverse=True)[:n]
        ]
