from datasette import hookimpl, Response
from datasette_llm_usage import LLM
from markupsafe import escape
from sqlite_migrate import Migrations
from sqlite_utils import Database
import json
import time

migration = Migrations("datasette-queries")


@migration()
def create_table(db):
    db["_datasette_queries"].create(
        {
            "slug": str,
            "title": str,
            "description": str,
            "sql": str,
            "actor": str,
            "created_at": int,
        },
        pk="slug",
    )


PROMPT = """
Suggest a title and description for this new SQL query.

The database is called "{database}" and it contains tables: {table_names}.

The SQL query is: {sql}

The title should be in "Sentence case". The description should be quite short.

Return the suggested title and description as JSON:
```json
{{"title": "Suggested title", "description": "Suggested description"}}
```
"""


@hookimpl
def canned_queries(datasette, database):
    async def inner():
        db = datasette.get_database(database)
        if await db.table_exists("_datasette_queries"):
            queries = {
                row["slug"]: {
                    "sql": row["sql"],
                    "title": row["title"],
                    "description": row["description"],
                }
                for row in await db.execute("select * from _datasette_queries")
            }
            return queries

    return inner


def extract_json(text):
    try:
        # Everything from first "{" to last "}"
        start = text.index("{")
        end = text.rindex("}")
        return json.loads(text[start : end + 1])
    except ValueError:
        return {}


def slugify(text):
    return "-".join(text.lower().split())


async def suggest_metadata(request, datasette):
    if request.method != "POST":
        return Response.json({"error": "POST request required"}, status=400)
    post_data = await request.post_vars()
    if "sql" not in post_data:
        return Response.json({"error": "sql parameter required"}, status=400)
    sql = post_data["sql"]
    llm = LLM(datasette)
    database = request.url_vars["database"]
    db = datasette.get_database(database)
    table_names = await db.table_names()
    prompt = PROMPT.format(
        table_names=" ".join(table_names),
        database=database,
        sql=sql,
    )
    model = llm.get_async_model("gpt-4o-mini")
    completion = await model.prompt(prompt, json_object=True, max_tokens=250)
    text = await completion.text()
    json_data = extract_json(text)
    if json_data:
        return Response.json(
            dict(
                json_data,
                url=slugify(json_data["title"]),
                usage=dict((await completion.usage()).__dict__),
                duration=await completion.duration_ms(),
                prompt=prompt,
            )
        )
    else:
        return Response.json(
            {
                "error": "No JSON data found in completion",
            },
            status=400,
        )


async def save_query(datasette, request):
    if not await datasette.permission_allowed(request.actor, "datasette-queries"):
        return Response.text("Permission denied", status=403)
    if request.method != "POST":
        return Response.json({"error": "POST request required"}, status=400)
    post_data = await request.post_vars()
    if "sql" not in post_data or "database" not in post_data or "url" not in post_data:
        datasette.add_message(
            request, "sql and database and url parameters required", datasette.ERROR
        )
        Response.redirect("/")
    sql = post_data["sql"]
    url = post_data["url"]
    database = post_data["database"]
    try:
        db = datasette.get_database(database)
    except KeyError:
        datasette.add_message(request, f"Database not found", datasette.ERROR)
        return Response.redirect("/")
    # Run migrations
    await db.execute_write_fn(lambda conn: migration.apply(Database(conn)))

    # TODO: Check if URL exists already
    await db.execute_write(
        """
        insert into _datasette_queries
            (slug, title, description, sql, actor, created_at)
        values
            (:slug, :title, :description, :sql, {actor}, :created_at)
    """.format(
            actor=":actor" if request.actor else "null"
        ),
        {
            "slug": url,
            "title": post_data.get("title", ""),
            "description": post_data.get("description", ""),
            "sql": sql,
            "actor": request.actor["id"] if request.actor else None,
            "created_at": int(time.time()),
        },
    )
    datasette.add_message(request, f"Query saved as {url}", datasette.INFO)
    return Response.redirect(datasette.urls.database(database) + "/" + url)


@hookimpl
def register_routes():
    return [
        (r"^/(?P<database>[^/]+)/-/suggest-title-and-description$", suggest_metadata),
        # /-/save-query
        (r"^/-/save-query$", save_query),
    ]


@hookimpl
def top_query(datasette, request, database, sql):
    async def inner():
        if sql and await datasette.permission_allowed(
            request.actor, "datasette-queries"
        ):
            return await datasette.render_template(
                "_datasette_queries_top.html",
                {
                    "sql": sql,
                    "database": database,
                },
                request=request,
            )

    return inner
