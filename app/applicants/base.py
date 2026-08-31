"""Common contract for read-only ATS application inspectors."""

from abc import ABC, abstractmethod

from playwright.async_api import Page

from app.schemas.application import ApplicationForm


class ApplicantAdapter(ABC):
    @abstractmethod
    async def inspect(self, page: Page, *, job_id: int) -> ApplicationForm:
        """Inspect a rendered form without changing its state."""
