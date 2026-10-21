import pytest

from leadengine.platform.rbac import allowed, normalize_path

AGENT_OK = [("GET", "/leads"), ("GET", "/leads/3"), ("GET", "/leads/3/matches"), ("GET", "/leads/3/next-actions"), ("GET", "/leads/3/deal-sheet"),
            ("POST", "/leads/3/status"), ("POST", "/leads/3/activities"), ("POST", "/leads/3/viewings"), ("POST", "/leads/3/outreach"),
            ("GET", "/tasks/today"), ("GET", "/viewings"), ("HEAD", "/leads"), ("GET", "/leads/")]
AGENT_NO = [("GET", "/agents"), ("POST", "/agents"), ("DELETE", "/leads/3"), ("POST", "/leads/3/consent"), ("POST", "/leads/3/assign"),
            ("GET", "/roi"), ("POST", "/spend"), ("GET", "/inventory"), ("POST", "/inventory"), ("GET", "/audit"), ("POST", "/ingest/signals"),
            ("POST", "/connectors/reddit/run"), ("POST", "/maintenance/escalate"), ("GET", "/stats"), ("GET", "/radar"), ("GET", "/markets/cities"),
            ("PATCH", "/leads/3"), ("POST", "/leads/abc/status"), ("GET", "/leads/3/unknown"), ("POST", "/demo/seed")]
MANAGER_OK = [("GET", "/agents"), ("POST", "/agents"), ("PATCH", "/agents/2"), ("POST", "/leads/3/consent"), ("POST", "/leads/3/assign"),
              ("GET", "/roi"), ("POST", "/spend"), ("POST", "/inventory"), ("GET", "/audit"), ("POST", "/ingest/csv"), ("POST", "/maintenance/escalate"),
              ("POST", "/maintenance/rescore"), ("GET", "/markets/cities"), ("GET", "/stats"), ("GET", "/leads"), ("POST", "/connectors/reddit/run"),
              ("GET", "/viewings.ics"), ("POST", "/tools/deal-costs")]
MANAGER_NO = [("DELETE", "/leads/3"), ("POST", "/maintenance/purge"), ("POST", "/demo/seed"), ("GET", "/anything-else"), ("POST", "/auth/invite"),
              ("DELETE", "/agents/2"), ("OPTIONS", "/leads")]
OWNER_OK = [("DELETE", "/leads/3"), ("POST", "/maintenance/purge"), ("POST", "/demo/seed"), ("GET", "/leads"), ("POST", "/agents")]


@pytest.mark.parametrize("m,p", AGENT_OK)
def test_agent_allowed(m, p):
    assert allowed("agent", m, p) and allowed("manager", m, p) and allowed("owner", m, p)


@pytest.mark.parametrize("m,p", AGENT_NO)
def test_agent_denied(m, p):
    assert not allowed("agent", m, p)


@pytest.mark.parametrize("m,p", MANAGER_OK)
def test_manager_allowed(m, p):
    assert allowed("manager", m, p) and allowed("owner", m, p)


@pytest.mark.parametrize("m,p", MANAGER_NO)
def test_manager_denied(m, p):
    assert not allowed("manager", m, p)


@pytest.mark.parametrize("m,p", OWNER_OK)
def test_owner_allowed_for_owner_only_routes(m, p):
    assert allowed("owner", m, p)


def test_unknown_route_and_method_are_owner_only():
    assert allowed("owner", "GET", "/brand-new-endpoint") and not allowed("manager", "GET", "/brand-new-endpoint")
    assert not allowed("owner", "OPTIONS", "/leads") and not allowed("owner", "TRACE", "/leads")


def test_unknown_role_is_denied_everything():
    assert not allowed("god", "GET", "/leads") and not allowed("", "GET", "/leads") and not allowed(None, "GET", "/leads")


@pytest.mark.parametrize("path", ["/leads/../agents", "//leads", "/leads/%2e%2e/agents", "/leads%2Fx", "/leads\\x", "/leads\x00", "/leads/./x",
                                  "leads", "", "/lëads", "/leads/\n", "/leads/%252e", "/" + "a" * 3000])
def test_suspicious_paths_rejected_for_every_role(path):
    assert normalize_path(path) is None
    for role in ("agent", "manager", "owner"):
        assert not allowed(role, "GET", path)


def test_normalisation_rules():
    assert normalize_path("/leads/") == "/leads" and normalize_path("/leads") == "/leads" and normalize_path("/") == "/"
    assert normalize_path("/leads//") is None
    assert allowed("agent", "GET", "/leads/") is True


def test_case_is_not_folded():
    assert not allowed("manager", "GET", "/Leads") and allowed("owner", "GET", "/Leads")      # unknown route: owner-only default
