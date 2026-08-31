import pytest
from app.applicants.detector import ATS, detect_ats


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://boards.greenhouse.io/acme", ATS.GREENHOUSE),
        ("https://job-boards.greenhouse.io/acme", ATS.GREENHOUSE),
        ("https://jobs.lever.co/acme", ATS.LEVER),
        ("https://acme.myworkdayjobs.com/en-US/jobs", ATS.WORKDAY),
        ("https://example.com/jobs", ATS.UNKNOWN),
    ],
)
def test_detect_ats(url: str, expected: ATS) -> None:
    assert detect_ats(url) is expected
