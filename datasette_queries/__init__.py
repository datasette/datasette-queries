from datasette import hookimpl, Response
from markupsafe import escape
from sqlite_migrate import Migrations
from sqlite_utils import Database
import json
import time

migration = Migrations("datasette-queries")


@migration()
def create_table(db):
    if db["_datasette_queries"].exists():
        return
    db["_datasette_queries"].create(
        {
            "slug": str,
            "database": str,
            "title": str,
            "description": str,
            "sql": str,
            "actor": str,
            "created_at": int,
        },
        pk="slug",
    )
    db["_datasette_queries"].create_index(["slug", "database"], unique=True)


@hookimpl
def canned_queries(datasette, database):
    async def inner():
        internal_db = datasette.get_internal_database()
        if await internal_db.table_exists("_datasette_queries"):
            queries = {
                row["slug"]: {
                    "sql": row["sql"],
                    "title": row["title"],
                    "description": row["description"],
                }
                for row in await internal_db.execute(
                    "select * from _datasette_queries where database = ?", [database]
                )
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


async def delete_query(datasette, request):
    if not await datasette.permission_allowed(request.actor, "datasette-queries"):
        return Response.text("Permission denied", status=403)
    if request.method != "POST":
        return Response.json({"error": "POST request required"}, status=400)
    data = json.loads((await request.post_body()).decode("utf8"))
    if "query_name" not in data or "db_name" not in data:
        return Response.redirect("/")
    query_name = data["query_name"]
    db_name = data["db_name"]

    await datasette.get_internal_database().execute_write(
        """
          delete from _datasette_queries
          where slug = :slug and database = :database
        """,
        {
            "slug": query_name,
            "database": db_name,
        },
    )
    datasette.add_message(request, f"Query deleted: {query_name}", datasette.WARNING)
    redirect_path = datasette.urls.database(db_name)
    if request.args.get("fetch"):
        return Response.json(
            {"message": "Query deleted", "redirect_path": redirect_path}
        )
    return Response.redirect(redirect_path)


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
    await datasette.get_internal_database().execute_write_fn(
        lambda conn: migration.apply(Database(conn))
    )

    # Check if URL exists already for this database
    db = datasette.get_database(database)

    async def url_in_use(url):
        if await db.table_exists(url):
            return True
        if await db.view_exists(url):
            return True
        return (
            len(
                (
                    await datasette.get_internal_database().execute(
                        "select 1 from _datasette_queries where slug = ?", [url]
                    )
                ).rows
            )
            > 0
        )

    prefix = 1
    original_url = url
    while await url_in_use(url):
        prefix += 1
        url = f"{original_url}_{prefix}"
        if prefix > 100:
            datasette.add_message(request, "URL is not available", datasette.ERROR)
            return Response.redirect("/")

    await datasette.get_internal_database().execute_write(
        """
        insert into _datasette_queries
            (slug, database, title, description, sql, actor, created_at)
        values
            (:slug, :database, :title, :description, :sql, {actor}, :created_at)
    """.format(
            actor=":actor" if request.actor else "null"
        ),
        {
            "slug": url,
            "database": database,
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
        (r"^/(?P<database>[^/]+)/-/datasette-queries/delete-query$", delete_query),
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


@hookimpl
def query_actions(datasette, actor, database, query_name, request, sql, params):
    if query_name is None:
        return []
    db_name = database
    js = f"""

    function run() {{
        const queryName={json.dumps(query_name)};
        const dbName={json.dumps(db_name)};
        if (confirm("Are you sure you want to delete this query?")) {{
            fetch(`{datasette.urls.database(database)}/-/datasette-queries/delete-query?fetch=1`, {{
                method: "POST",
                headers: {{
                    "Content-Type": "application/json",
                }},
                body: JSON.stringify({{
                    query_name: queryName,
                    db_name: dbName
                }})

            }}).then(response => {{
                if (response.ok) {{
                    response.text().then(text => {{
                        window.location.href = JSON.parse(text).redirect_path;
                    }});
                }} else {{
                    alert("Failed to delete query");
                }}
            }});
        }}
    }}
    run();
    """
    return [
        {
            "label": "Delete query",
            "description": "",
            "href": f"javascript:{(js)}",
        }
    ]
