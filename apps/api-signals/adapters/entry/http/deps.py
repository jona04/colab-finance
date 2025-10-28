# apps/api-signals/adapters/entry/http/deps.py
from fastapi import Request
from motor.motor_asyncio import AsyncIOMotorDatabase

def get_db(request: Request) -> AsyncIOMotorDatabase:
    return request.app.state.db
