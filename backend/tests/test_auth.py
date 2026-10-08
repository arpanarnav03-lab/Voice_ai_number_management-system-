def test_no_password_means_no_login(h):
    assert h.client.get("/api/auth/status").json()["required"] is False
    assert h.client.get("/api/agents").status_code == 200


def test_password_protects_api(make_harness):
    h = make_harness("s3cret")
    assert h.client.get("/api/agents").status_code == 401
    assert h.client.post("/api/auth/login", json={"password": "nope"}).status_code == 401
    r = h.client.post("/api/auth/login", json={"password": "s3cret"})
    assert r.status_code == 200
    assert "httponly" in r.headers["set-cookie"].lower() and "samesite=lax" in r.headers["set-cookie"].lower()
    assert h.client.get("/api/agents").status_code == 200
    assert h.client.get("/api/auth/status").json() == {"required": True, "authenticated": True, "mode": "fake"}
