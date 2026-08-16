import importlib.util as u

for p in ("fastapi", "uvicorn", "pydantic", "yaml"):
    print(f"{p:12} {'OK' if u.find_spec(p) else 'ABSENT'}")
