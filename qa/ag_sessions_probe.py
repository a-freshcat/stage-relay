import sqlite3, os, json
p = os.path.join(os.path.expanduser("~"), ".gemini", "antigravity", "conversation_summaries.db")
c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
rows = c.execute(
    "SELECT conversation_id, title, step_count, last_modified_time, "
    "workspace_uris, status, source, project_id, app_data_dir, killed "
    "FROM conversation_summaries ORDER BY last_modified_time DESC").fetchall()
print("total conversations on disk:", len(rows))
print()
qa = "ag-native"
for r in rows:
    cid, title, steps, modified, ws, status, source, proj, appdir, killed = r
    mark = " <== QA workspace" if ws and qa in ws else ""
    print(f"{cid}")
    print(f"   title={title!r} steps={steps} status={status} source={source}")
    print(f"   modified={modified} project={proj} app_data_dir={appdir} killed={killed}")
    print(f"   workspace={ws}{mark}")
print()
hit = [r for r in rows if r[4] and qa in r[4]]
print("matching our qa/ag-native workspace:", len(hit))
c.close()