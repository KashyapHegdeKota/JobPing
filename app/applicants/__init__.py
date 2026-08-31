"""Applicant inspection adapters."""

from app.applicants.detector import ATS, detect_ats
from app.applicants.greenhouse import GreenhouseApplicant

__all__ = ["ATS", "GreenhouseApplicant", "detect_ats"]
