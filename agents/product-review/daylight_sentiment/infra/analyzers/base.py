"""Provider-agnostic analyzer interface."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


class SentimentAnalyzer(ABC):
    @abstractmethod
    def analyze_product_review(self, text: str, product_name: Optional[str] = None,
                               rating: Optional[int] = None) -> Dict[str, Any]:
        ...

    @abstractmethod
    def analyze_service_review(self, text: str, rating: Optional[int] = None) -> Dict[str, Any]:
        ...

    def analyze_batch(self, reviews: List[Dict[str, Any]], review_type: str
                      ) -> List[Optional[Dict[str, Any]]]:
        """Analyse a batch. Default implementation falls back to one call each."""
        return [self._analyze_one(r, review_type) for r in reviews]

    def _analyze_one(self, review: Dict[str, Any], review_type: str
                     ) -> Optional[Dict[str, Any]]:
        text = (review.get("text") or "").strip()
        if not text:
            return None
        rating = review.get("rating")
        if review_type == "product":
            return self.analyze_product_review(
                text, product_name=review.get("product_name") or None, rating=rating)
        return self.analyze_service_review(text, rating=rating)
