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
        {"slug": str, "title": str, "description": str, "sql": str, "actor": str, "created_at": int},
        pk="slug"
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
                } for row in await db.execute("select * from _datasette_queries")
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
    return '-'.join(text.lower().split())


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
    if request.method != "POST":
        return Response.json({"error": "POST request required"}, status=400)
    post_data = await request.post_vars()
    if "sql" not in post_data or "database" not in post_data or "url" not in post_data:
        datasette.add_message(request, "sql and database and url parameters required", datasette.ERROR)
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
    await db.execute_write("""
        insert into _datasette_queries
            (slug, title, description, sql, actor, created_at)
        values
            (:slug, :title, :description, :sql, {actor}, :created_at)
    """.format(actor = "{actor}" if request.actor else "null"), {
        "slug": url,
        "title": post_data.get("title", ""),
        "description": post_data.get("description", ""),
        "sql": sql,
        "actor": request.actor["id"] if request.actor else None,
        "created_at": int(time.time())
    })
    datasette.add_message(request, f"Query saved as {url}", datasette.INFO)
    return Response.redirect(datasette.urls.database(database) + '/' + url)


@hookimpl
def register_routes():
    return [
        (r"^/(?P<database>[^/]+)/-/suggest-title-and-description$", suggest_metadata),
        # /-/save-query
        (r"^/-/save-query$", save_query),
    ]


CSS = """
<style>
.grid-form {
    font-family: system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    line-height: 1.6;
    max-width: 800px;
    background-color: white;
    padding: 1.5rem;
    border-radius: 8px;
    box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1);
    box-sizing: border-box;

    & * {
        box-sizing: border-box;
    }

    & .form-grid {
        display: grid;
        grid-template-columns: 8em 1fr;
        gap: 1rem;
        align-items: start;
    }

    & label {
        font-weight: 500;
        color: #333;
    }

    & input[type="text"],
    & textarea {
        width: 100%;
        padding: 0.5rem;
        border: 1px solid #ddd;
        border-radius: 4px;
        font-size: 1rem;
        transition: border-color 0.2s;

        &:focus {
            outline: none;
            border-color: #4a90e2;
            box-shadow: 0 0 0 2px rgba(74, 144, 226, 0.1);
        }
    }

    & textarea {
        min-height: 100px;
        resize: vertical;
    }

    & .submit-container {
        grid-column: 2;
        margin-top: 0.5rem;
    }

    & button {
        background-color: #4a90e2;
        color: white;
        border: none;
        padding: 0.5rem 1.5rem;
        border-radius: 4px;
        cursor: pointer;
        font-size: 1rem;
        transition: background-color 0.2s;

        &:hover {
            background-color: #357abd;
        }
    }

    @media (max-width: 600px) {
        padding: 1rem;

        & .form-grid {
            grid-template-columns: 1fr;
            gap: 0.75rem;
        }

        & .submit-container {
            grid-column: 1;
        }
    }
}
</style>
"""

JS = """
<script>
const details = document.querySelector('details.save-this-query');
const titleInput = document.querySelector('#title');
const urlInput = document.querySelector('#url');
details.addEventListener('toggle', () => {
    if (details.open) {
        titleInput.focus();
    }
});
let urlManuallyEdited = false;

// Function to convert title to URL-friendly slug
function generateSlug(text) {
    return text
        .toLowerCase()
        .replace(/[^a-z0-9\\s-]/g, '') // Remove non-alphanumeric chars (except spaces and hyphens)
        .replace(/\\s+/g, '-')         // Replace spaces with hyphens
        .replace(/-+/g, '-')          // Replace multiple hyphens with single hyphen
        .trim();                      // Remove leading/trailing spaces
}

// Listen for changes to the title field
titleInput.addEventListener('input', () => {
    if (!urlManuallyEdited) {
        urlInput.value = generateSlug(titleInput.value);
    }
});

// Detect manual URL edits
urlInput.addEventListener('input', (e) => {
    // Only mark as manually edited if the change wasn't from our script
    if (e.inputType) {  // inputType exists for real user input
        urlManuallyEdited = true;
    }
    // Unless they blank it out
    if (urlInput.value === '') {
        urlManuallyEdited = false;
    }
});

async function fetchSuggestions(sql) {
  try {
    const csrfToken = document.querySelector('[name="csrftoken"]').value;
    const formData = new URLSearchParams();
    formData.append('sql', sql);
    formData.append('csrftoken', csrfToken);
    const response = await fetch('/content/-/suggest-title-and-description', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/x-www-form-urlencoded',
      },
      body: formData.toString()
    });
    if (!response.ok) {
      throw new Error(`HTTP error! status: ${response.status}`);
    }
    const data = await response.json();
    return data;
  } catch (error) {
    console.error('Error fetching suggestions:', error);
    throw error;
  }
}

// Get the suggest link element
const suggestLink = document.getElementById('suggest-title');
const originalText = suggestLink.innerText; // Save original text

// Add click event listener
suggestLink.addEventListener('click', async (e) => {
  e.preventDefault(); // Prevent default link behavior
  
  // Disable the link and show loading state
  suggestLink.style.pointerEvents = 'none';
  suggestLink.innerHTML = 'Suggesting...';
  
  try {
    // Get SQL value from textarea
    const sql = document.querySelector('textarea[name="sql"]').value;
    
    // Call the fetchSuggestions function
    const suggestions = await fetchSuggestions(sql);
    
    // Update form fields with returned data
    document.querySelector('input[name="title"]').value = suggestions.title || '';
    document.querySelector('textarea[name="description"]').value = suggestions.description || '';
    document.querySelector('input[name="url"]').value = suggestions.url || '';
    
    // Reset link state
    suggestLink.innerHTML = originalText;
    suggestLink.style.pointerEvents = 'auto';
    suggestLink.style.color = ''; // Reset to default color
    
  } catch (error) {
    // Handle error state
    suggestLink.innerHTML = error.message;
    suggestLink.style.color = 'red';
    suggestLink.style.pointerEvents = 'auto'; // Re-enable link to allow retry
  }
});
</script>
"""


@hookimpl
def top_query(datasette, request, database, sql):
    if sql:
        return (
            CSS
            + """
<details class="save-this-query"><summary>Save this query</summary>
    <div class="grid-form">
        <p style="margin-top: 0.5em">Saved queries can be used by everyone else with access to the <strong>{database}</strong> database.</p>
        <form class="core" action="/-/save-query" method="post">
            <div class="form-grid">
                <label for="title">Title</label>
                <div>
                    <input type="text" id="title" name="title" required>
                    <a href="#" id="suggest-title" style="text-decoration: none">Suggest title and description ✨</a>
                </div>

                <label for="url">URL</label>
                <input type="text" id="url" name="url" required>

                <label for="description">Description</label>
                <textarea id="description" name="description"></textarea>

                <div class="submit-container">
                    <button type="submit">Save query</button>
                </div>
            </div>
            <input type="hidden" name="sql" value="{sql}">
            <input type="hidden" name="csrftoken" value="{csrftoken}">
            <input type="hidden" name="database" value="{database}">
        </form>
    </div>
</details>
        """.format(
                sql=escape(sql),
                database=escape(database),
                csrftoken=request.scope["csrftoken"](),
            )
            + JS
        )
