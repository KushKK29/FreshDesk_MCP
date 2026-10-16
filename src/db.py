"""Single shared MongoClient for this connector, so oauth_server.py and
auth.py (and any future module) open one connection pool against
freshdesk_connector instead of each opening its own — same database, same
URI, no reason for two independent pools that could silently diverge in
connection options later.
"""
import os

from pymongo import MongoClient

MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")

_mongo = MongoClient(MONGO_URI)
db = _mongo["freshdesk_connector"]
