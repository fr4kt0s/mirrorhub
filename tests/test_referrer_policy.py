"""HTML form submissions must retain same-origin information."""

from fastapi.testclient import TestClient


def test_login_uses_same_origin_referrer_policy(
    client: TestClient,
) -> None:
    response = client.get("/login")
    assert response.status_code == 200
    assert response.headers["referrer-policy"] == "same-origin"
