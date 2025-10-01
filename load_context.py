import json
from pathlib import Path

def load_context():
    """Load content from context directory into a single string."""
    context_dir = Path("context")
    context_dir.mkdir(exist_ok=True)

    all_content = ""
    for file_path in context_dir.glob("*"):
        if file_path.is_file():
            try:
                if file_path.suffix == ".json":
                    with open(file_path, "r") as f:
                        content = json.dumps(json.load(f), indent=2)
                else:
                    content = file_path.read_text(encoding="utf-8")
                all_content += f"\n=== {file_path.name} ===\n{content}\n"
            except Exception:
                pass

    return all_content.strip() or "No context files found."