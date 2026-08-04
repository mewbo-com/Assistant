"""The I/O edge: read a manifest, render every artifact, report what changed.

Everything above this module is pure. This is the one place that walks a
directory, writes files, and decides that a mismatch between the source tree
and the manifest is fatal.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from .frame import FrameStyle, WindowFramer
from .manifest import FrameManifest

_RENDERABLE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".svg"}


class FrameRunner(BaseModel):
    """Renders a manifest's artifacts from a source tree into a published tree.

    Cost class: O(declared artifacts), each O(canvas area). Bounded by the
    manifest, never by whatever happens to be sitting in the directory.
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    manifest_path: Path
    source_dir: Path
    output_dir: Path
    style: FrameStyle = Field(default_factory=FrameStyle)

    def _audit(self, manifest: FrameManifest) -> None:
        """Fail on any drift between the source directory and the manifest.

        Both directions are errors. An undeclared file means a new capture
        landed with nobody deciding whether it is a window or a closeup, and
        defaulting that choice would silently publish the wrong picture. A
        declared file with no source means the manifest outlived its artifact.
        """
        present = {
            path.name
            for path in self.source_dir.iterdir()
            if path.is_file()
            and path.suffix.lower() in _RENDERABLE_SUFFIXES
            and not path.name.startswith("._")
        }
        declared = manifest.declared_files()

        if undeclared := sorted(present - declared):
            raise ValueError(
                "source files with no manifest entry (classify each as "
                f"'window' or 'passthrough' in {self.manifest_path.name}): "
                + ", ".join(undeclared)
            )
        if missing := sorted(declared - present):
            raise FileNotFoundError(
                f"manifest declares files absent from {self.source_dir}: " + ", ".join(missing)
            )

    def run(self) -> list[str]:
        """Render every declared artifact and return one report line each.

        One framer is prepared per named canvas and reused across every
        artifact that names it, so the wallpaper is decoded and cover-fitted
        once per shape rather than once per file.
        """
        manifest = FrameManifest.load(self.manifest_path)
        self._audit(manifest)

        wallpaper = (self.manifest_path.parent / manifest.wallpaper).resolve()
        framers = {
            name: WindowFramer.load(wallpaper, style)
            for name, style in manifest.resolve_canvases(self.style).items()
        }
        self.output_dir.mkdir(parents=True, exist_ok=True)

        return [
            artifact.render(self.source_dir, self.output_dir, framers)
            for artifact in manifest.artifacts
        ]
