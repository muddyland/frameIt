#!/usr/bin/env python3
"""
One-time migration: imports data.json (photos + trailers) into the SQLite database.
Safe to run multiple times — skips existing records.

Usage:
    cd /path/to/frameit
    source .venv/bin/activate
    python scripts/migrate_data.py
"""
import os
import sys
import json

# Allow importing from the project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import app
from models import db, Poster, Trailer

DATA_DIR = os.environ.get("DATA_DIR", "./config")
IMAGES_DIR = os.environ.get("IMAGES_DIR", "./images")
JSON_FILE = os.path.join(DATA_DIR, "data.json")


def load_json():
    try:
        with open(JSON_FILE, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"No data.json found at {JSON_FILE} — nothing to migrate.")
        return None
    except json.JSONDecodeError as e:
        print(f"Failed to parse data.json: {e}")
        return None


def migrate():
    data = load_json()
    if not data:
        return

    with app.app_context():
        db.create_all()

        # Migrate photos
        photos = data.get("photos", [])
        migrated_posters = 0
        for photo in photos:
            # path is stored as /images/filename — extract bare filename
            raw_path = photo.get("path", "")
            filename = os.path.basename(raw_path)
            if not filename:
                continue
            exists = Poster.query.filter_by(filename=filename).first()
            if exists:
                print(f"  [skip] poster already exists: {filename}")
                continue
            poster = Poster(filename=filename, active=True)
            db.session.add(poster)
            migrated_posters += 1

        # Migrate trailers
        trailers = data.get("trailers", [])
        migrated_trailers = 0
        for trailer in trailers:
            youtube_id = trailer.get("id", "")
            title = trailer.get("name", youtube_id)
            if not youtube_id:
                continue
            exists = Trailer.query.filter_by(youtube_id=youtube_id).first()
            if exists:
                print(f"  [skip] trailer already exists: {youtube_id}")
                continue
            t = Trailer(youtube_id=youtube_id, title=title, active=True)
            db.session.add(t)
            migrated_trailers += 1

        db.session.commit()
        print(f"Migration complete: {migrated_posters} posters, {migrated_trailers} trailers imported.")
        print(f"You can now archive {JSON_FILE} — it is no longer used.")


if __name__ == "__main__":
    migrate()
