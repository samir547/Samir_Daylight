"""Provider-agnostic analyzer interface."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Union

from daylight_sentiment.infra.analyzers.errors import FatalProviderError


class SentimentAnalyzer(ABC):
    """One implementation per LLM provider (registered in factory.py).

    Provider failures surface as the typed errors in errors.py; in particular
    a FatalProviderError (bad key, no credit, unknown model) stops the run.
    """

    @abstractmethod
    def complete_json(self, prompt: str, max_tokens: Optional[int] = None) -> Dict[str, Any]:
        """Send one prompt and return the model's reply parsed as a JSON
        object. The sentiment methods and the translation pass both go
        through this. ``max_tokens`` caps the reply; None means the
        provider's default for short classification output. A reply cut off
        at that cap raises ResponseTruncatedError."""
        ...

    @abstractmethod
    def analyze_product_review(self, text: str, product_name: Optional[str] = None,
                               rating: Optional[int] = None) -> Dict[str, Any]:
        ...

    @abstractmethod
    def analyze_service_review(self, text: str, rating: Optional[int] = None) -> Dict[str, Any]:
        ...

    def analyze_batch(self, reviews: List[Dict[str, Any]], review_type: str
                      ) -> List[Union[Dict[str, Any], Exception, None]]:
        """Analyse a batch, returning one entry per input review, in order:
        the analysis dict, None if there was nothing to analyse, or the
        exception that failed that review alone. A FatalProviderError is
        raised, not returned, so the run stops. Default implementation makes
        one call per review."""
        results: List[Union[Dict[str, Any], Exception, None]] = []
        for review in reviews:
            try:
                results.append(self._analyze_one(review, review_type))
            except FatalProviderError:
                raise
            except Exception as exc:  # noqa: BLE001
                results.append(exc)
        return results

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
