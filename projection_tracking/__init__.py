"""
Projection Tracking module for Construction Operations API.
Calculates monthly financial projections (EAC, cost variance, billing position)
per active job using HCSS HeavyJob data.

New module — added alongside budget_tracking and quantity_tracking.
"""
from projection_tracking.analyzer import ProjectionResult, ProjectionStatus, calculate_projections

__all__ = ["ProjectionResult", "ProjectionStatus", "calculate_projections"]