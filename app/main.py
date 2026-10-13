from fastapi import FastAPI

from app.api import auth_routes, metrics, review, submissions
from app.logging_setup import setup_logging

setup_logging()
app = FastAPI(title="AI Project Evaluation Platform (POC)", version="0.1.0")
app.include_router(auth_routes.router)
app.include_router(submissions.router)
app.include_router(review.router)
app.include_router(metrics.router)
