"""CORS must allow the live PortaGov origins, not only localhost.

Verified live 2026-11-27: the Render backend answered no
Access-Control-Allow-Origin for https://portagov.giin.co.rw and
https://portagov-frontend.vercel.app, only for http://localhost:3000.
The live site calls the backend through the same-origin /pg-api proxy,
so this did not break the live site, but any direct browser call from
the live origins was refused.
"""

import pytest

from app import create_app
from app.config import Config


@pytest.fixture()
def client(tmp_path):
    app = create_app({"SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path}/t.db", "TESTING": True,
                      "UPLOAD_DIR": str(tmp_path / "up")})
    return app.test_client()


def test_default_cors_origins_include_live_origins():
    assert "https://portagov.giin.co.rw" in Config.CORS_ORIGINS
    assert "https://portagov-frontend.vercel.app" in Config.CORS_ORIGINS
    assert "http://localhost:3000" in Config.CORS_ORIGINS


@pytest.mark.parametrize("origin", ["https://portagov.giin.co.rw",
                                    "https://portagov-frontend.vercel.app"])
def test_preflight_allows_live_origin(client, origin):
    r = client.options("/api/health", headers={"Origin": origin,
                                                "Access-Control-Request-Method": "GET"})
    assert r.headers.get("Access-Control-Allow-Origin") == origin
