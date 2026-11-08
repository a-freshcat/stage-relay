import sqlite3, json, time, os
p = r"data\workspace.sqlite3"
c = sqlite3.connect(p)
print("mtime:", time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(p))))
for r in c.execute("select seq,project,kind,actor,body,created from events order by seq"):
    print(f"seq={r[0]} kind={r[2]} actor={r[3]} created={r[5]}")
    print("   ", r[4])
