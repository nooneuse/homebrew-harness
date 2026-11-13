"""External collection status is information; an explicit wait has a durable wake condition."""

import json


def ready(db, conn, run):
    wait = run.get("source_wait")
    if wait is None:
        return True
    if wait["until"] == "event":
        if conn.execute("SELECT count(*) FROM inbox WHERE recipient=?", ("reference_" + run["id"],)).fetchone()[0]:
            return True
        return db._get(conn, "input_epochs", "reference_" + run["id"], 0) > wait["after_input"]
    sources = [json.loads(row[0]) for row in conn.execute(
        "SELECT value FROM records WHERE section='run_sources' AND owner=?", ("lead:" + run["session_id"],))]
    return bool(sources) and all(not source.get("failures", 0)
        and source.get("revision", 0) > wait["after_sources"].get(source["id"], -1) for source in sources)
