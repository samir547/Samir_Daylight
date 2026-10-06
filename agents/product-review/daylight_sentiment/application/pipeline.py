"""The analysis orchestrator: 2D branching over (review_type x review_source)."""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional

from daylight_sentiment.application.checkpoint import CheckpointWriter
from daylight_sentiment.config import Config
from daylight_sentiment.infra.analyzers.base import SentimentAnalyzer

logger = logging.getLogger("multi_source_sentiment")


class MultiSourcePipeline:
    """2D branching pipeline: review_type x review_source."""

    def __init__(self, config: Config, analyzer: SentimentAnalyzer):
        self.config = config
        self.analyzer = analyzer
        self._lock = threading.Lock()
        self._done = 0
        self._ckpt: Optional[CheckpointWriter] = None

    def process_review(self, review: Dict[str, Any]) -> Dict[str, Any]:
        review_type = review.get("review_type")
        text = (review.get("text") or "").strip()

        if not text:
            return {**review, "analysis": None, "error": "Empty review text"}
        text = text[: self.config.max_review_chars]
        rating = review.get("rating")

        try:
            if review_type == "product":
                analysis = self.analyzer.analyze_product_review(
                    text, product_name=review.get("product_name") or None, rating=rating)
            elif review_type == "service":
                analysis = self.analyzer.analyze_service_review(text, rating=rating)
            else:
                return {**review, "analysis": None,
                        "error": f"Unknown review_type: {review_type}"}
            return {**review, "analysis": analysis, "error": None}
        except Exception as exc:  # noqa: BLE001
            logger.error("Processing failed for %s: %s", review.get("uid"), exc)
            return {**review, "analysis": None, "error": str(exc)}

    def _record(self, result: Dict[str, Any], total: int) -> None:
        with self._lock:
            self._done += 1
            if self._ckpt:
                self._ckpt.write(result)
            if self._done % 100 == 0 or self._done == total:
                logger.info("Processed %d/%d reviews", self._done, total)

    def process_batch(self, batch: List[Dict[str, Any]], review_type: str
                      ) -> List[Dict[str, Any]]:
        """Analyse one batch, returning a result dict per input review."""
        try:
            analyses = self.analyzer.analyze_batch(batch, review_type)
        except Exception as exc:  # noqa: BLE001
            logger.error("Batch failed outright: %s", exc)
            analyses = [None] * len(batch)

        out = []
        for review, analysis in zip(batch, analyses):
            if analysis is None:
                out.append({**review, "analysis": None,
                            "error": "Model returned no analysis for this review"})
            else:
                out.append({**review, "analysis": analysis, "error": None})
        return out

    def process_all_reviews(self, reviews: List[Dict[str, Any]],
                            checkpoint_path: Optional[Path] = None,
                            done_uids: Optional[Dict[str, Dict[str, Any]]] = None,
                            resume: Optional[bool] = None
                            ) -> List[Dict[str, Any]]:
        """``resume`` decides whether the checkpoint file is appended to
        (True) or truncated (False). It defaults to "done_uids is non-empty",
        which was correct while every run loaded every source -- but a run
        that loads only some sources (or none, when nothing new is staged in
        SharePoint) restricts done_uids to those sources, and an empty
        done_uids must NOT wipe the other sources' checkpoint entries. The
        CLI passes resume explicitly (= not --fresh-start) for that reason.
        """
        done_uids = done_uids or {}
        if resume is None:
            resume = bool(done_uids)
        pending = [r for r in reviews if r["uid"] not in done_uids]
        results: List[Dict[str, Any]] = list(done_uids.values())
        if done_uids:
            logger.info("Resuming: %d already analysed, %d to go",
                        len(done_uids), len(pending))

        # Reviews with no text never reach the model.
        empties = [r for r in pending if not (r.get("text") or "").strip()]
        pending = [r for r in pending if (r.get("text") or "").strip()]
        for r in empties:
            results.append({**r, "analysis": None, "error": "Empty review text"})
        if empties:
            logger.info("Skipped %d reviews with empty text", len(empties))

        # Batches must be type-homogeneous: product and service use different
        # prompts and different classification vocabularies.
        batches: List[tuple] = []
        size = max(1, self.config.batch_size)
        for review_type in ("product", "service"):
            subset = [r for r in pending if r.get("review_type") == review_type]
            for i in range(0, len(subset), size):
                batches.append((subset[i:i + size], review_type))

        unknown = [r for r in pending if r.get("review_type") not in ("product", "service")]
        for r in unknown:
            results.append({**r, "analysis": None,
                            "error": f"Unknown review_type: {r.get('review_type')}"})

        logger.info("Dispatching %d reviews in %d batches of up to %d",
                    len(pending), len(batches), size)

        self._ckpt = (CheckpointWriter(checkpoint_path, resume=resume)
                      if checkpoint_path else None)
        try:
            with ThreadPoolExecutor(max_workers=self.config.max_workers) as pool:
                futures = [pool.submit(self.process_batch, b, t) for b, t in batches]
                for fut in as_completed(futures):
                    for res in fut.result():
                        results.append(res)
                        self._record(res, len(pending))
        finally:
            if self._ckpt:
                self._ckpt.close()
                self._ckpt = None

        logger.info("Total processed: %d", len(results))
        return results
