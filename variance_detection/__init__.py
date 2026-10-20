"""
Variance Detection — Phase 3 risk monitoring module.

Reads from existing budget and quantity DB tables, computes variance
metrics, and exposes them via FastAPI routes.

Modules:
    detector  — core engine, DB queries, variance calculations
    schemas   — Pydantic request/response models
"""
