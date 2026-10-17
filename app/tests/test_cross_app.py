"""跨应用跳转的约定（app/domain/cross_app.py）：链接带来的参数不可信；回跳地址只从配置里来。"""

from __future__ import annotations

import httpx
import pytest

import app.interfaces.http.middleware.auth as auth_middleware
from app.domain.cross_app import (
    Origin,
    Source,
    Target,
    clean_origin,
    clean_ref,
    parse_sources,
    parse_targets,
    return_url,
)
from app.interfaces.http.web.cross_app import cross_app_settings
from app.main import app
from core.config import Settings
from tests.test_auth_routes_security import FakeAuthService, session

KNOWN = frozenset({"knowbench", "knowbase"})
SOURCES = (
    '{"knowbench": {"return_url": '
    '"https://knowbench.example.test/zh-CN/workbench/back?ref={ref}"},'
    ' "knowbase": {}}'
)
TARGETS = (
    '{"knowfeed": {"web_base_url": "https://knowfeed.example.test/"},'
    ' "knowbase": {"web_base_url": "https://knowbase.example.test"}}'
)


# ---------------- 链接带来的参数 ----------------
@pytest.mark.parametrize(
    ("app_name", "ref", "expected"),
    [
        ("knowbench", "task:42", Origin("knowbench", "task:42")),
        (
            "knowbench",
            "01a0ed51-828f-7b13",
            Origin("knowbench", "01a0ed51-828f-7b13"),
        ),
        ("knowbase", None, Origin("knowbase", None)),
        # 引用不合规则：应用还认，引用当作没带
        ("knowbench", "bad ref", Origin("knowbench", None)),
        ("knowbench", "a/b", Origin("knowbench", None)),
        ("knowbench", "x" * 129, Origin("knowbench", None)),
        ("knowbench", "", Origin("knowbench", None)),
        ("knowbench", 42, Origin("knowbench", None)),
        ("knowbench", "https://evil.example.test", Origin("knowbench", None)),
        # 不认识的应用：整个当作没带
        ("unknown", "x", None),
        ("Knowbench", "x", None),
        ("knowbench ", "x", None),
        ("", "x", None),
        (None, "x", None),
        (["knowbench"], "x", None),
    ],
)
def test_what_a_link_brings_is_not_trusted(app_name, ref, expected):
    assert clean_origin(app_name, ref, known_apps=KNOWN) == expected


def test_a_reference_is_opaque_and_short():
    assert clean_ref("A-z.0_9:x") == "A-z.0_9:x"
    assert clean_ref("x" * 128) == "x" * 128
    for bad in (
        "",
        " ",
        "x" * 129,
        "a b",
        "a?b",
        "a&b",
        "a#b",
        "a%20b",
        "引用",
        None,
        1,
    ):
        assert clean_ref(bad) is None


# ---------------- 回到原处 ----------------
def test_the_way_back_comes_from_configuration_only():
    sources = parse_sources(SOURCES)
    back = return_url(sources["knowbench"], Origin("knowbench", "task:42"))
    assert back == "https://knowbench.example.test/zh-CN/workbench/back?ref=task%3A42"
    # 没带引用：回跳地址照给，引用的位置是空的
    assert (
        return_url(sources["knowbench"], Origin("knowbench"))
        == "https://knowbench.example.test/zh-CN/workbench/back?ref="
    )
    # 没登记回跳地址的应用：没有「回到原处」
    assert return_url(sources["knowbase"], Origin("knowbase", "x")) is None
    assert return_url(None, Origin("knowbench", "x")) is None
    assert return_url(sources["knowbench"], None) is None
    # 张冠李戴：不给
    assert return_url(sources["knowbench"], Origin("knowbase", "x")) is None


# ---------------- 配置 ----------------
def test_the_two_settings():
    assert parse_sources(SOURCES) == {
        "knowbench": Source(
            "knowbench",
            "https://knowbench.example.test/zh-CN/workbench/back?ref={ref}",
        ),
        "knowbase": Source("knowbase"),
    }
    assert parse_targets(TARGETS) == {
        "knowfeed": Target("knowfeed", "https://knowfeed.example.test"),
        "knowbase": Target("knowbase", "https://knowbase.example.test"),
    }
    assert parse_sources("{}") == {} and parse_targets("") == {}
    blank = Settings(_env_file=None)
    assert blank.cross_app_sources() == {} and blank.cross_app_targets() == {}
    set_up = Settings(
        _env_file=None,
        cross_app_sources_json=SOURCES,
        cross_app_targets_json=TARGETS,
    )
    assert set(set_up.cross_app_sources()) == {"knowbench", "knowbase"}
    assert (
        set_up.cross_app_targets()["knowfeed"].web_base_url
        == "https://knowfeed.example.test"
    )


@pytest.mark.parametrize(
    "bad",
    [
        "not json",
        "[]",
        '{"Knowbench": {}}',
        '{"in vestment": {}}',
        '{"knowbench": []}',
        '{"knowbench": {"return_to": "https://a.example.test"}}',
        '{"knowbench": {"return_url": 1}}',
        '{"knowbench": {"return_url": "/relative?ref={ref}"}}',
        '{"knowbench": {"return_url": "javascript:alert(1)"}}',
        '{"knowbench": {"return_url": "ftp://a.example.test/{ref}"}}',
        '{"knowbench": {"return_url": "https://u:p@a.example.test/?ref={ref}"}}',
        '{"knowbench": {"return_url": "https://a.example.test/{ref}/{ref}"}}',
        '{"knowbench": {"return_url": "https://a.example.test/{other}"}}',
        '{"knowbench": {"return_url": "https://{ref}.example.test/"}}',
        '{"knowbench": {"return_url": "https://a.example.test/ x?ref={ref}"}}',
        '{"knowbench": {"return_url": "https://a.example.test/#ref={ref}"}}',
        '{"knowbench": {"return_url": "https://a.example.test:port/?ref={ref}"}}',
    ],
)
def test_a_wrong_list_of_sources_stops_the_start(bad):
    with pytest.raises(ValueError):
        parse_sources(bad)
    with pytest.raises(ValueError):
        Settings(_env_file=None, cross_app_sources_json=bad)


@pytest.mark.parametrize(
    "bad",
    [
        "not json",
        '{"knowfeed": {}}',
        '{"knowfeed": {"web_base_url": ""}}',
        '{"knowfeed": {"web_base_url": "knowfeed.example.test"}}',
        '{"knowfeed": {"web_base_url": "https://knowfeed.example.test/?a=1"}}',
        '{"knowfeed": {"web_base_url": "https://u:p@knowfeed.example.test"}}',
        '{"knowfeed": {"web_base_url": "https://knowfeed.example.test", "token": "x"}}',
        '{"INFO": {"web_base_url": "https://knowfeed.example.test"}}',
    ],
)
def test_a_wrong_list_of_targets_stops_the_start(bad):
    with pytest.raises(ValueError):
        parse_targets(bad)
    with pytest.raises(ValueError):
        Settings(_env_file=None, cross_app_targets_json=bad)


# ---------------- 接口 ----------------
@pytest.fixture
def signed_in(monkeypatch: pytest.MonkeyPatch):
    fake = FakeAuthService({"member": session("web", "profile:read")})
    monkeypatch.setattr(auth_middleware, "web_auth_service", fake)
    app.dependency_overrides[cross_app_settings] = lambda: Settings(
        _env_file=None,
        app_slug="knowmart",
        cross_app_sources_json=SOURCES,
        cross_app_targets_json=TARGETS,
    )
    try:
        yield
    finally:
        app.dependency_overrides.pop(cross_app_settings, None)


def client(*, cookie: bool = True) -> httpx.AsyncClient:
    http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    )
    if cookie:
        http.cookies.set("sunmoonai_knowmart_web_sid", "member")
    return http


@pytest.mark.asyncio
async def test_where_this_application_takes_people(signed_in):
    async with client() as http:
        got = await http.get("/api/web/v1/cross-app/links")
    assert got.status_code == 200
    assert got.json() == {
        "app": "knowmart",
        "targets": {
            "knowfeed": {"web_base_url": "https://knowfeed.example.test"},
            "knowbase": {"web_base_url": "https://knowbase.example.test"},
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("params", "expected"),
    [
        (
            {"from": "knowbench", "ref": "task:42"},
            {
                "app": "knowbench",
                "ref": "task:42",
                "return_url": (
                    "https://knowbench.example.test/zh-CN/workbench/back?ref=task%3A42"
                ),
            },
        ),
        (
            {"from": "knowbase", "ref": "x"},
            {"app": "knowbase", "ref": "x", "return_url": None},
        ),
        (
            {"from": "knowbench", "ref": "bad ref"},
            {
                "app": "knowbench",
                "ref": None,
                "return_url": "https://knowbench.example.test/zh-CN/workbench/back?ref=",
            },
        ),
        ({"from": "unknown", "ref": "x"}, None),
        ({"ref": "x"}, None),
        ({}, None),
    ],
)
async def test_where_the_person_came_from(signed_in, params, expected):
    async with client() as http:
        got = await http.get("/api/web/v1/cross-app/origin", params=params)
    assert got.status_code == 200
    assert got.json() == expected


@pytest.mark.asyncio
async def test_an_address_in_the_link_is_never_used(signed_in):
    async with client() as http:
        got = await http.get(
            "/api/web/v1/cross-app/origin",
            params={
                "from": "knowbench",
                "ref": "t1",
                "return_to": "https://evil.example.test/",
                "return_url": "https://evil.example.test/",
                "redirect": "https://evil.example.test/",
            },
        )
    assert got.status_code == 200
    assert "evil" not in got.text


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["links", "origin?from=knowbench"])
async def test_only_people_who_signed_in(signed_in, path):
    async with client(cookie=False) as http:
        got = await http.get(f"/api/web/v1/cross-app/{path}")
    assert got.status_code == 401


def test_nothing_here_changes_anything():
    methods = {
        method
        for route in app.routes
        if getattr(route, "path", "").startswith("/api/web/v1/cross-app")
        for method in getattr(route, "methods", set())
    }
    assert methods == {"GET"}
