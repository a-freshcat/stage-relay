import sys, sqlite3, json
sys.path.insert(0, ".")
from relay.storage import default_data_dir, describe
print("旧默认（python app.py 用的 ROOT/data）:", describe(__import__("pathlib").Path("data")))
print("新默认（现在统一解析）:", describe(default_data_dir()))
