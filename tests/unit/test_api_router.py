"""Tests for versioned API router registration."""

from app.main import create_app


def test_create_app_mounts_versioned_http_routes() -> None:
    application = create_app(startup_health_checks=False)
    route_paths = set(application.openapi()["paths"])

    assert "/api/v1/jobs" in route_paths
    assert "/api/v1/companies" in route_paths
    assert "/api/v1/stats" in route_paths
    assert "/api/v1/sse/feed" in route_paths
