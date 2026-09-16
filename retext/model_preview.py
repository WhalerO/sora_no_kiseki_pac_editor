"""Compatibility exports for the model3d geometry and CPU preview module.

New code should import from :mod:`retext.model3d` or
:mod:`retext.model3d.geometry`.  This facade keeps the former public import
path working without leaving the implementation coupled to the package root.
"""

from .model3d.geometry import *  # noqa: F401,F403
