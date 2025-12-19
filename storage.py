import os

BASE_PATH = "thumbnails/ndvi"

def save_file(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(content)

def public_url(path):
    # Replace with CDN / S3 / Supabase Storage later
    return f"/static/{path}"
