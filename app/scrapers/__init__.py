"""Source-specific clients for ingesting job data."""

from app.scrapers.applyguy import ApplyGuyFeed, ApplyGuyScraper
from app.scrapers.github_client import GitHubClient

__all__ = ["ApplyGuyFeed", "ApplyGuyScraper", "GitHubClient"]
