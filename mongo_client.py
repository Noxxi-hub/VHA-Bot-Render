# ════════════════════════════════════════════════
#  mongo_client.py  •  VHA Übersetzer-Bot
#  Zentrale MongoDB-Verbindung (ersetzt SQLite,
#  weil Render kein persistentes Dateisystem hat)
# ════════════════════════════════════════════════

import os
import logging

from pymongo import MongoClient
from pymongo.server_api import ServerApi

log = logging.getLogger("VHATranslator.Mongo")

# Name der Datenbank innerhalb des Clusters.
# Die URI selbst enthält keinen DB-Namen (mongodb+srv://.../?appName=...),
# deshalb wird er hier fest vergeben.
_DB_NAME = os.getenv("MONGODB_DB_NAME", "vha_translate_bot")

_client: MongoClient | None = None
_db = None


def get_db():
    """
    Gibt die (gecachte) MongoDB-Datenbankverbindung zurück.
    Baut die Verbindung beim ersten Aufruf auf.
    """
    global _client, _db

    if _db is not None:
        return _db

    uri = os.getenv("MONGODB_URI")
    if not uri:
        raise RuntimeError(
            "MONGODB_URI fehlt! Bitte als Umgebungsvariable setzen "
            "(lokal in .env, auf Render im Dashboard unter 'Environment')."
        )

    _client = MongoClient(uri, server_api=ServerApi("1"), serverSelectionTimeoutMS=8000)
    # Verbindung sofort testen, damit Fehler früh & klar auftauchen
    _client.admin.command("ping")
    _db = _client[_DB_NAME]
    log.info(f"✅ MongoDB verbunden (Datenbank: {_DB_NAME})")
    return _db
