"""Public API for the flatten_music utility and its tests."""

from .flatten_music import (
    Metadata,
    build_plan,
    collision_safe_name,
    execute_plan,
    music_filename,
    render_report,
    scan_tree,
)

__all__ = [
    "Metadata",
    "build_plan",
    "collision_safe_name",
    "execute_plan",
    "music_filename",
    "render_report",
    "scan_tree",
]
