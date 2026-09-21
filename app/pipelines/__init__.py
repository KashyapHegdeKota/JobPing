"""Ingestion pipeline orchestration."""

from app.pipelines.applyguy_pipeline import ApplyGuyPipeline
from app.pipelines.simplify_pipeline import SimplifyPipeline

__all__ = ["ApplyGuyPipeline", "SimplifyPipeline"]
