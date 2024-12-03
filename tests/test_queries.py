from datasette.app import Datasette
import sqlite_utils
import pytest


@pytest.mark.asyncio
async def test_save_query(tmpdir):
    db_path = str(tmpdir / "data.db")
    sqlite_utils.Database(db_path).vacuum()
    datasette = Datasette(
        [db_path], config={"permissions": {"datasette-queries": {"id": "*"}}}
    )
    response = await datasette.client.get("/data/-/query?sql=select+21")
    assert response.status_code == 200
    assert "<summary>" in response.text
    # Submit the form
    response2 = await datasette.client.post(
        "/-/save-query",
        data={"sql": "select 21", "url": "select-21", "database": "data"},
    )
    assert response2.status_code == 302
    # Should have been saved
    response3 = await datasette.client.get("/data/select-21.json?_shape=array")
    data = response3.json()
    assert data == [{"21": 21}]
