from datasette.app import Datasette
import sqlite_utils
import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("authed", (True,))
async def test_save_query(tmpdir, authed):
    db_path = str(tmpdir / "data.db")
    sqlite_utils.Database(db_path).vacuum()
    datasette = Datasette(
        [db_path], config={"permissions": {"datasette-queries": {"id": "*"}}}, pdb=True
    )
    cookies = {}
    if authed:
        cookies = {"ds_actor": datasette.client.actor_cookie({"id": "user"})}
    response = await datasette.client.get(
        "/data/-/query?sql=select+21", cookies=cookies
    )
    assert response.status_code == 200
    if authed:
        assert "<summary>" in response.text
    else:
        assert "<summary>" not in response.text

    csrftoken = ""
    if "ds_csrftoken" in response.cookies:
        csrftoken = response.cookies["ds_csrftoken"]
        cookies["ds_csrftoken"] = response.cookies["ds_csrftoken"]

    # Submit the form
    response2 = await datasette.client.post(
        "/-/save-query",
        data={
            "sql": "select 21",
            "url": "select-21",
            "database": "data",
            "csrftoken": csrftoken,
        },
        cookies=cookies,
    )
    if authed:
        assert response2.status_code == 302
    else:
        assert response2.status_code == 403
        return

    # Should have been saved
    response3 = await datasette.client.get("/data/select-21.json?_shape=array")
    data = response3.json()
    assert data == [{"21": 21}]
    assert (
        await datasette.get_internal_database().execute(
            "select actor, database, slug, description, sql from _datasette_queries"
        )
    ).dicts() == [
        {
            "actor": "user",
            "database": "data",
            "description": "",
            "slug": "select-21",
            "sql": "select 21",
        },
    ]

    response4 = await datasette.client.post(
        "/data/-/datasette-queries/delete-query",
        json={
            "db_name": "data",
            "query_name": "select-21",
            "csrftoken": csrftoken,
        },
        cookies=cookies,
    )
    assert response4.status_code == 302

    assert (
        await datasette.get_internal_database().execute(
            "select actor, database, slug, description, sql from _datasette_queries"
        )
    ).dicts() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "object_type, original_slug",
    (
        ("table", "fruits"),
        ("view", "animals"),
    ),
)
async def test_save_query_name_collision_with_table_or_view(
    tmpdir, object_type, original_slug
):
    # Create a database file that already contains either a table or a view that
    # will clash with the query name we try to save
    db_path = str(tmpdir / "data.db")
    db = sqlite_utils.Database(db_path)

    if object_type == "table":
        # Create a table called "fruits"
        db[original_slug].insert({"id": 1})
    else:
        # Create a view called "animals"
        db.conn.execute(f"create view {original_slug} as select 1 as one")
        db.conn.commit()

    # Spin up Datasette with the plugin enabled and the required permission
    datasette = Datasette(
        [db_path], config={"permissions": {"datasette-queries": {"id": "*"}}}
    )

    # Authenticate so that we are allowed to save a query
    cookies = {"ds_actor": datasette.client.actor_cookie({"id": "user"})}

    # Hit the canned-query page to obtain a CSRF token
    r1 = await datasette.client.get(f"/data/-/query?sql=select+1", cookies=cookies)
    csrftoken = r1.cookies.get("ds_csrftoken")
    cookies["ds_csrftoken"] = csrftoken

    # Attempt to save a query using a slug that clashes with the existing table/view
    r2 = await datasette.client.post(
        "/-/save-query",
        data={
            "sql": "select 1",
            "url": original_slug,
            "database": "data",
            "csrftoken": csrftoken,
        },
        cookies=cookies,
    )
    # We should be redirected after a successful save
    assert r2.status_code == 302

    # The plugin should have renamed the slug to "<slug>_2"
    expected_slug = f"{original_slug}_2"

    # Saved canned query should be reachable and execute correctly
    r3 = await datasette.client.get(f"/data/{expected_slug}.json?_shape=array")
    assert r3.status_code == 200
    assert r3.json() == [{"1": 1}]

    # Verify the internal _datasette_queries table contains the new slug
    rows = (
        await datasette.get_internal_database().execute(
            "select slug, sql from _datasette_queries"
        )
    ).dicts()
    assert rows == [{"slug": expected_slug, "sql": "select 1"}]

    # Add the same query a second time to confirm it gets _3
    r4 = await datasette.client.post(
        "/-/save-query",
        data={
            "sql": "select 1",
            "url": original_slug,
            "database": "data",
            "csrftoken": csrftoken,
        },
        cookies=cookies,
    )
    assert r4.status_code == 302

    # The plugin should have renamed the slug to "<slug>_3"
    expected_slug = f"{original_slug}_3"

    # Saved canned query should be reachable and execute correctly
    r5 = await datasette.client.get(f"/data/{expected_slug}.json?_shape=array")
    assert r5.status_code == 200
    assert r5.json() == [{"1": 1}]
