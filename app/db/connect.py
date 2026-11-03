"""Create the real Supabase client with the service key (the only key that reads or writes)."""

from supabase import create_client

from app.config.settings import Settings
from app.db.client import Database


def create_db(settings: Settings) -> Database:
    return create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_KEY)  # type: ignore[return-value]
