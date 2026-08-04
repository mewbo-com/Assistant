"""Composite raw doc captures onto a 16:9 wallpaper canvas.

`docs/assets/img-src/` holds the sources: raw Playwright captures from the demo
pipeline plus every hand-capture no spec produces. `docs/assets/img/` holds
what the docs and the README actually reference. This package is the one
transform between them, so the published look is re-tunable by re-running it
rather than by re-capturing anything.
"""

from .frame import FrameStyle, WindowFramer
from .manifest import FrameManifest, PassthroughArtifact, WindowArtifact
from .runner import FrameRunner
from .video import VideoFramer

__all__ = [
    "FrameManifest",
    "FrameRunner",
    "FrameStyle",
    "PassthroughArtifact",
    "VideoFramer",
    "WindowArtifact",
    "WindowFramer",
]
