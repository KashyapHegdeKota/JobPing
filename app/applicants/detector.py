"""ATS URL detection."""

from enum import StrEnum
from urllib.parse import urlparse


class ATS(StrEnum):
    GREENHOUSE = "greenhouse"
    LEVER = "lever"
    WORKDAY = "workday"
    UNKNOWN = "unknown"


def detect_ats(url: str) -> ATS:
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    if host in {"boards.greenhouse.io", "job-boards.greenhouse.io"} or host.endswith(
        ".greenhouse.io"
    ):
        return ATS.GREENHOUSE
    if host == "lever.co" or host.endswith(".lever.co"):
        return ATS.LEVER
    if host.endswith(".myworkdayjobs.com") or host == "myworkdayjobs.com":
        return ATS.WORKDAY
    return ATS.UNKNOWN
