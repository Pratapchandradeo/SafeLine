# load_context.py
import json
from pathlib import Path

def load_context():
    """Load content from context directory - simplified version"""
    context_dir = Path("context")
    context_dir.mkdir(exist_ok=True)
    
    # Look for JSON files specifically
    json_files = list(context_dir.glob("*.json"))
    
    if not json_files:
        return "No context files found."
    
    # Use the first JSON file found
    json_file = json_files[0]
    try:
        with open(json_file, 'r') as f:
            content = json.load(f)
        return json.dumps(content, indent=2)
    except Exception as e:
        return f"Error loading {json_file}: {str(e)}"