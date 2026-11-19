"""Tests for local API security guards:
Strict Host/Origin/Referer validation and explicit CSRF policy.
Implements the mandatory 17-point test matrix from Stage 2.
"""
import pytest
from server import app


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


class TestHostHeaderValidation:
    """DNS rebinding prevention: only 127.0.0.1, localhost, and [::1] are permitted loopback authorities."""

    def test_loopback_hosts_permitted(self, client):
        for host in ["127.0.0.1:5000", "localhost:5000", "[::1]:5000", "127.0.0.1", "localhost"]:
            resp = client.get("/", headers={"Host": host})
            assert resp.status_code == 200, f"Host {host} should be accepted"

    def test_dns_rebinding_hosts_rejected(self, client):
        bad_hosts = [
            "evil.com",
            "evil.com:5000",
            "attacker.local",
            "192.168.1.100",
            "victim-site.org:5000",
            "malicious.attacker.io",
            "localhost.attacker.com",
            "127.0.0.1.attacker.com",
            "attacker:password@127.0.0.1:5000",
            "127.0.0.1:99999",
        ]
        for host in bad_hosts:
            resp = client.get("/", headers={"Host": host})
            assert resp.status_code == 400
            assert "Invalid Host header" in resp.get_json().get("error", "")

    def test_dns_rebinding_rejected_on_mutating_api(self, client):
        resp = client.post("/api/reset", headers={"Host": "evil.com:5000", "X-QA-Request": "1"})
        assert resp.status_code == 400
        assert "Invalid Host header" in resp.get_json().get("error", "")


class TestMandatorySecurityMatrix:
    """17-point test matrix covering Origin, Referer, Sec-Fetch-Site and explicit CSRF headers."""

    # 1. Giltig Host och exakt Origin -> Når endpoint
    def test_1_valid_host_and_exact_origin(self, client):
        resp = client.post("/api/reset", headers={
            "Host": "127.0.0.1:5000",
            "Origin": "http://127.0.0.1:5000",
            "Sec-Fetch-Site": "same-origin",
        })
        assert resp.status_code == 200

    # 2. Giltig samma-origin Referer utan Origin -> Når endpoint
    def test_2_valid_same_origin_referer_without_origin(self, client):
        resp = client.post("/api/reset", headers={
            "Host": "127.0.0.1:5000",
            "Referer": "http://127.0.0.1:5000/dashboard?tab=1",
            "Sec-Fetch-Site": "same-origin",
        })
        assert resp.status_code == 200

    # 3. Ingen Origin/Referer, X-QA-Request: 1 -> Når endpoint
    def test_3_headerless_with_explicit_qa_request_header(self, client):
        resp = client.post("/api/reset", headers={
            "Host": "127.0.0.1:5000",
            "X-QA-Request": "1",
        })
        assert resp.status_code == 200

    # 4. Ingen Origin/Referer/header vid POST -> 403
    def test_4_mutating_without_origin_referer_or_header_rejected(self, client):
        resp = client.post("/api/reset", headers={
            "Host": "127.0.0.1:5000",
        })
        assert resp.status_code == 403
        assert "explicit local-client header required" in resp.get_json().get("error", "")

    # 5. Origin: null, tom Origin eller felaktig Origin -> 403
    def test_5_null_empty_or_malformed_origin_rejected(self, client):
        for bad_origin in ["null", "", "http://", "not-a-url"]:
            resp = client.post("/api/reset", headers={
                "Host": "127.0.0.1:5000",
                "Origin": bad_origin,
            })
            assert resp.status_code == 403

    # 6. Annan lokal port eller loopback-alias -> 403
    def test_6_cross_port_or_loopback_alias_rejected(self, client):
        # Different port on same host
        resp1 = client.post("/api/reset", headers={
            "Host": "127.0.0.1:5000",
            "Origin": "http://127.0.0.1:9000",
        })
        assert resp1.status_code == 403
        assert "Cross-origin Origin is prohibited" in resp1.get_json().get("error", "")

        # Mixed localhost vs 127.0.0.1
        resp2 = client.post("/api/reset", headers={
            "Host": "127.0.0.1:5000",
            "Origin": "http://localhost:5000",
        })
        assert resp2.status_code == 403
        assert "Cross-origin Origin is prohibited" in resp2.get_json().get("error", "")

    # 7. Annat schema -> 403
    def test_7_different_scheme_rejected(self, client):
        resp = client.post("/api/reset", headers={
            "Host": "127.0.0.1:5000",
            "Origin": "https://127.0.0.1:5000",
        })
        assert resp.status_code == 403
        assert "Cross-origin Origin is prohibited" in resp.get_json().get("error", "")

    # 8. Främmande Host / Host med suffix, userinfo eller ogiltig port -> 400
    def test_8_foreign_or_invalid_host_rejected(self, client):
        for bad_host in ["evil.com", "127.0.0.1:99999", "admin:pass@127.0.0.1"]:
            resp = client.get("/api/projects", headers={"Host": bad_host})
            assert resp.status_code == 400
            assert "Invalid Host header" in resp.get_json().get("error", "")

    # 9. Giltig Origin + främmande/relativ/tom Referer -> 403
    def test_9_valid_origin_with_invalid_or_foreign_referer_rejected(self, client):
        for bad_ref in ["https://evil.com/page", "/relative/path", ""]:
            resp = client.post("/api/reset", headers={
                "Host": "127.0.0.1:5000",
                "Origin": "http://127.0.0.1:5000",
                "Referer": bad_ref,
            })
            assert resp.status_code == 403

    # 10. Främmande Origin + giltig Referer eller specialheader -> 403
    def test_10_foreign_origin_with_valid_referer_or_special_header_rejected(self, client):
        resp = client.post("/api/reset", headers={
            "Host": "127.0.0.1:5000",
            "Origin": "http://evil.com",
            "Referer": "http://127.0.0.1:5000/dashboard",
            "X-QA-Request": "1",
        })
        assert resp.status_code == 403
        assert "Origin" in resp.get_json().get("error", "")

    # 11. Origin med path/query/fragment, flera origins, backslash/kontrolltecken -> 403
    def test_11_origin_with_path_query_fragment_or_special_chars_rejected(self, client):
        for bad_origin in [
            "http://127.0.0.1:5000/path",
            "http://127.0.0.1:5000?query=1",
            "http://127.0.0.1:5000#frag",
            "http://127.0.0.1:5000 http://evil.com",
            "http://127.0.0.1:5000\\evil",
            "http://127.0.0.1:5000\x00",
        ]:
            resp = client.post("/api/reset", headers={
                "Host": "127.0.0.1:5000",
                "Origin": bad_origin,
            })
            assert resp.status_code == 403

    # 12. cross-site -> 403
    def test_12_sec_fetch_site_cross_site_rejected(self, client):
        resp = client.post("/api/reset", headers={
            "Host": "127.0.0.1:5000",
            "Sec-Fetch-Site": "cross-site",
        })
        assert resp.status_code == 403
        assert "Cross-site requests are prohibited" in resp.get_json().get("error", "")

    # 13. Muterande same-site -> 403
    def test_13_mutating_same_site_rejected(self, client):
        resp = client.post("/api/reset", headers={
            "Host": "127.0.0.1:5000",
            "Sec-Fetch-Site": "same-site",
            "Origin": "http://127.0.0.1:5000",
        })
        assert resp.status_code == 403
        assert "Cross-origin same-site writes are prohibited" in resp.get_json().get("error", "")

    # 14. Headerlös skrivning med enbart Sec-Fetch-Site: same-origin -> 403
    def test_14_headerless_write_with_only_sec_fetch_site_rejected(self, client):
        resp = client.post("/api/reset", headers={
            "Host": "127.0.0.1:5000",
            "Sec-Fetch-Site": "same-origin",
        })
        assert resp.status_code == 403
        assert "explicit local-client header required" in resp.get_json().get("error", "")

    # 15. Standardport implicit respektive explicit (t.ex. HTTP 80) -> Samma origin
    def test_15_standard_port_normalization(self, client):
        resp = client.post("/api/reset", headers={
            "Host": "127.0.0.1",
            "Origin": "http://127.0.0.1:80",
            "Sec-Fetch-Site": "same-origin",
        })
        assert resp.status_code == 200

    # 16. Direkt GET till UI utan ursprungshuvuden -> Fungerar
    def test_16_direct_get_ui_without_origin_permitted(self, client):
        resp = client.get("/", headers={"Host": "127.0.0.1:5000"})
        assert resp.status_code == 200

    # 17. Cross-origin OPTIONS med Access-Control-Request-Headers: X-QA-Request -> Ingen CORS-auktorisering
    def test_17_cross_origin_options_preflight_receives_no_cors_headers(self, client):
        resp = client.open(
            "/api/reset",
            method="OPTIONS",
            headers={
                "Host": "127.0.0.1:5000",
                "Origin": "http://evil.com",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "X-QA-Request",
            }
        )
        assert resp.headers.get("Access-Control-Allow-Origin") is None
        assert resp.headers.get("Access-Control-Allow-Headers") is None
