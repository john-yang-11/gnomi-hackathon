"""
Crawl statistics tracker — tracks success, blocks, dupes, quality.
"""
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class CrawlStats:
    started_at: str = field(default_factory=lambda: datetime.now().isoformat())
    total_fetched: int = 0
    total_blocked: int = 0
    total_skipped_classifier: int = 0
    total_extracted: int = 0
    total_duplicate: int = 0
    total_low_quality: int = 0
    quality_scores: list[float] = field(default_factory=list)
    block_reasons: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    dupe_reasons: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    domains_crawled: set[str] = field(default_factory=set)

    def record_fetch(self, domain: str, blocked: bool, reason: str | None = None) -> None:
        self.total_fetched += 1
        self.domains_crawled.add(domain)
        if blocked:
            self.total_blocked += 1
            if reason:
                self.block_reasons[reason] += 1

    def record_skip(self) -> None:
        self.total_skipped_classifier += 1

    def record_extract(self, quality_score: float) -> None:
        self.total_extracted += 1
        self.quality_scores.append(quality_score)

    def record_duplicate(self, reason: str) -> None:
        self.total_duplicate += 1
        key = reason.split(":")[0]
        self.dupe_reasons[key] += 1

    def record_low_quality(self) -> None:
        self.total_low_quality += 1

    def summary(self) -> dict:
        fetched = self.total_fetched or 1
        extracted = self.total_extracted or 1
        avg_quality = (
            sum(self.quality_scores) / len(self.quality_scores)
            if self.quality_scores else 0.0
        )
        return {
            "started_at": self.started_at,
            "domains_crawled": len(self.domains_crawled),
            "total_fetched": self.total_fetched,
            "success_rate": f"{(fetched - self.total_blocked) / fetched:.1%}",
            "block_rate": f"{self.total_blocked / fetched:.1%}",
            "block_reasons": dict(self.block_reasons),
            "skipped_by_classifier": self.total_skipped_classifier,
            "articles_extracted": self.total_extracted,
            "duplicates_removed": self.total_duplicate,
            "duplicate_rate": f"{self.total_duplicate / max(self.total_extracted + self.total_duplicate, 1):.1%}",
            "dupe_breakdown": dict(self.dupe_reasons),
            "low_quality_dropped": self.total_low_quality,
            "avg_quality_score": round(avg_quality, 3),
            "quality_distribution": {
                "high (>0.7)": sum(1 for s in self.quality_scores if s > 0.7),
                "medium (0.4–0.7)": sum(1 for s in self.quality_scores if 0.4 <= s <= 0.7),
                "low (<0.4)": sum(1 for s in self.quality_scores if s < 0.4),
            },
        }

    def print_summary(self) -> None:
        import json
        print("\n" + "=" * 50)
        print("  CleanCrawl — Crawl Summary")
        print("=" * 50)
        print(json.dumps(self.summary(), indent=2))
        print("=" * 50)
