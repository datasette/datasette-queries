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
