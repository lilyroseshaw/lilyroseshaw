import httpx

from app.research_search import BraveSearchBackend, search_hits_to_candidates, site_scoped_query


def test_site_scoped_query_includes_domain_and_deletion_signals():
    query = site_scoped_query("Shop Example", "shopexample.com")
    assert "site:shopexample.com" in query
    assert "CCPA" in query


def test_brave_backend_parses_results_and_sends_api_key():
    seen_headers = {}

    def handler(request):
        seen_headers.update(request.headers)
        return httpx.Response(
            200,
            json={"web": {"results": [{"url": "https://shopexample.com/privacy", "title": "Privacy", "description": "d"}]}},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    backend = BraveSearchBackend("secret-key", client=client)
    hits = backend.search("site:shopexample.com privacy")

    assert seen_headers.get("x-subscription-token") == "secret-key"
    assert len(hits) == 1
    assert hits[0].url == "https://shopexample.com/privacy"


def test_brave_backend_returns_empty_on_error_status():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(401)))
    backend = BraveSearchBackend("bad-key", client=client)
    assert backend.search("anything") == []


def test_brave_backend_returns_empty_on_network_error():
    def handler(request):
        raise httpx.ConnectError("boom", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    backend = BraveSearchBackend("key", client=client)
    assert backend.search("anything") == []


def test_search_hits_become_candidates_but_are_not_pre_trusted():
    from app.research_search import SearchHit

    hits = [SearchHit(url="https://blog.someoneelse.com/x", title="Review", snippet="s")]
    candidates = search_hits_to_candidates(hits)
    assert candidates[0].discovered_via == "search:brave"
    # A search hit is only a candidate - verify_recipe still has to accept
    # or reject it based on domain, independent of where it came from.
