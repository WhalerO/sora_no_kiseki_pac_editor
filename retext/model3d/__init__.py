"""Stable boundary for Falcom MDL parsing, posing and viewport rendering.

The text/archive application imports this package instead of depending on the
software renderer's implementation module.  This keeps the optional 3D
preview subsystem replaceable without leaking it into TBL/DAT business code.
"""

from .service import Model3DService, model_3d_service
from .animation import (
    ModelAnimationClip,
    ModelAnimationKeyframe,
    ModelAnimationNodeTransform,
    ModelAnimationPlayer,
    ModelAnimationPose,
    ModelAnimationTrack,
    load_model_animation,
    load_model_node_transforms,
    sample_animation_track,
)
from .association import (
    animation_model_family,
    companion_model_entry,
    companion_model_path,
    direct_companion_model_entry,
)
from .identity import (
    ModelCompanionResolution,
    ModelIdentityMatch,
    ModelIdentityResolution,
    ModelIdentityService,
    model_identity_service,
)
from .viewport import calculate_render_dimensions
from .geometry import (
    ModelGeometry,
    ModelMaterialRenderInfo,
    ModelMaterialSurface,
    ModelRenderCancelled,
    ModelSkeleton,
)

__all__ = [
    "Model3DService",
    "ModelAnimationClip",
    "ModelAnimationKeyframe",
    "ModelAnimationNodeTransform",
    "ModelAnimationPlayer",
    "ModelAnimationPose",
    "ModelAnimationTrack",
    "ModelCompanionResolution",
    "ModelIdentityMatch",
    "ModelIdentityResolution",
    "ModelIdentityService",
    "ModelGeometry",
    "ModelMaterialRenderInfo",
    "ModelMaterialSurface",
    "ModelRenderCancelled",
    "ModelSkeleton",
    "calculate_render_dimensions",
    "animation_model_family",
    "companion_model_entry",
    "companion_model_path",
    "direct_companion_model_entry",
    "load_model_animation",
    "load_model_node_transforms",
    "model_3d_service",
    "model_identity_service",
    "sample_animation_track",
]
