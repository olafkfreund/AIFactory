"""
BMad Method Integration for AIFactory.

This module provides:
- Complexity detection (5 levels: 0-4)
- Track recommendation (Quick Flow, Standard, Enterprise)
- Agent persona enhancements
- Story-based planning support
- Session segmentation for token optimization
"""

from .complexity_detector import ComplexityDetector, ComplexityResult, Track
from .session_config import (
    disable_session_segmentation,
    enable_session_segmentation,
    is_session_segmentation_enabled,
    is_session_segmentation_enabled_for_spec,
)
from .track_config import (
    TrackConfig,
    describe_track,
    get_phases_for_track,
    get_track_config,
)

__all__ = [
    # Complexity Detection
    "ComplexityDetector",
    "ComplexityResult",
    "Track",
    # Track Configuration
    "TrackConfig",
    "get_track_config",
    "get_phases_for_track",
    "describe_track",
    # Session Segmentation
    "is_session_segmentation_enabled",
    "is_session_segmentation_enabled_for_spec",
    "enable_session_segmentation",
    "disable_session_segmentation",
]
