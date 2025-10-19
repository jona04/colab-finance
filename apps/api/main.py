from fastapi import FastAPI
from .routes import health, vaults, strategies

def create_app() -> FastAPI:
    app = FastAPI(title="DEX Vault API", version="0.1.0")
    app.include_router(health.router, prefix="/api")
    app.include_router(vaults.router, prefix="/api")
    app.include_router(strategies.router, prefix="/api")
    return app

app = create_app()
