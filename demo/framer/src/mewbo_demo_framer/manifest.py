"""What each doc artifact is, and how it is derived from its source.

The manifest is the trust boundary between a hand-edited JSON file and the
renderer, so it is strict: unknown keys are refused, and an artifact sitting in
the source tree with no entry here is an error rather than a silent skip. That
is deliberate. A new capture landing in `docs/assets/img-src/` must be
classified by a human, because the classification is a judgement about what the
picture shows, and defaulting it either way would quietly ship the wrong thing.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Literal

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field

from .frame import FrameStyle, WindowFramer

DEFAULT_CANVAS = "wide"
"""The 16:9 canvas every web capture uses."""

_JPEG_SUFFIXES = {".jpg", ".jpeg"}


class ArtifactBase(BaseModel):
    """Fields and behaviour shared by every treatment."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    file: str = Field(min_length=1)
    """Basename, identical in the source and published directories. The docs
    reference this name, so it is the one thing a treatment may never change."""

    note: str = ""
    """Why this artifact carries the treatment it does. Prose for the next
    reader, never parsed."""

    def render(
        self, source_dir: Path, output_dir: Path, framers: Mapping[str, WindowFramer]
    ) -> str:
        """Produce the published artifact and return a one-line outcome."""
        raise NotImplementedError

    def _source(self, source_dir: Path) -> Path:
        """Resolve and verify this artifact's source file."""
        path = source_dir / self.file
        if not path.is_file():
            raise FileNotFoundError(f"no source for {self.file!r} at {path}")
        return path

    @staticmethod
    def _encode(image: Image.Image, target: Path) -> None:
        """Write `image` to `target` with settings pinned for byte-determinism.

        Encoder settings are spelled out rather than left to Pillow's defaults
        because the pipeline's zero-diff gate compares committed bytes across
        runs; a default that moves with a Pillow upgrade would read as a UI
        change. Format follows the target's extension, because the docs already
        reference these basenames and a treatment may not rename them.
        """
        if target.suffix.lower() in _JPEG_SUFFIXES:
            image.save(target, format="JPEG", quality=90, subsampling=0, optimize=True)
            return
        image.save(target, format="PNG", optimize=True, compress_level=9)


class WindowArtifact(ArtifactBase):
    """A capture composited onto a named canvas as a rounded, contained window.

    Both a whole window and a single card qualify; what the canvas decides is
    what sits BEHIND it. A window belongs on a wallpaper because that is where
    an application lives, and a card lifted out of the app belongs on the app's
    own background because that is where the card lived.
    """

    treatment: Literal["window"] = "window"

    canvas: str = DEFAULT_CANVAS
    """Which named canvas from the manifest to compose onto.

    A web capture is landscape and belongs on the 16:9 canvas. A phone capture
    is portrait, and on a 16:9 canvas it shrinks to about a quarter of the
    width; it belongs on the squarer 4:3 canvas instead. An element crop
    belongs on the 4:3 canvas that mattes it in a flat colour. Naming the
    canvas here rather than deriving it from the source's own aspect ratio
    keeps the choice a human decision, the same way the treatment itself is.
    """

    def render(
        self, source_dir: Path, output_dir: Path, framers: Mapping[str, WindowFramer]
    ) -> str:
        """Composite the source onto its named wallpaper canvas."""
        source = self._source(source_dir)
        framer = framers[self.canvas]
        with Image.open(source) as handle:
            framed = framer.frame(handle)
        self._encode(framed, output_dir / self.file)
        return f"window/{self.canvas:<8} {self.file}  {framed.width}x{framed.height}"


class PassthroughArtifact(ArtifactBase):
    """An artifact published byte-for-byte as it arrives.

    Animated GIFs (this is a still-image pipeline), terminal SVGs that already
    draw their own window chrome, captures the owner composited by hand before
    this package existed, and any source so tall that containing it on a canvas
    would shrink it past reading. Copying rather than skipping keeps one rule
    true for every published file: everything in the published directory was
    produced from the source directory by this manifest.
    """

    treatment: Literal["passthrough"] = "passthrough"

    def render(
        self, source_dir: Path, output_dir: Path, framers: Mapping[str, WindowFramer]
    ) -> str:
        """Copy the source through unchanged."""
        source = self._source(source_dir)
        shutil.copyfile(source, output_dir / self.file)
        return f"passthru          {self.file}"


Artifact = Annotated[
    WindowArtifact | PassthroughArtifact,
    Field(discriminator="treatment"),
]


class FrameManifest(BaseModel):
    """The full set of published artifacts and their treatments."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    wallpaper: str = Field(min_length=1)
    """Path to the backdrop, relative to the manifest file."""

    canvases: dict[str, FrameStyle] = Field(default_factory=dict)
    """Named canvas styles a `window` artifact may compose onto.

    Empty means "use the runner's own style under the default name", which is
    what keeps a minimal manifest working. The shipped manifest declares three:
    a 16:9 `wide` for web captures, a 4:3 `portrait` for phone captures, and a
    4:3 `closeup` for element crops, which differs from `portrait` in style
    rather than geometry — a solid matte and no shadow. All three are authored
    at the same HEIGHT so the shapes sit together in a docs page without one
    looking taller than the others.
    """

    artifacts: list[Artifact] = Field(min_length=1)

    @classmethod
    def load(cls, path: Path) -> FrameManifest:
        """Parse and validate a manifest from disk."""
        return cls.model_validate(json.loads(path.read_text(encoding="utf-8")))

    def declared_files(self) -> set[str]:
        """Every basename this manifest speaks for."""
        return {artifact.file for artifact in self.artifacts}

    def canvas(self, name: str, fallback: FrameStyle | None = None) -> FrameStyle:
        """One named canvas style, refusing a name this manifest does not define.

        For a caller naming a canvas directly rather than through an artifact
        entry — the video compositor is the one today. Refusing beats
        defaulting for exactly the reason `resolve_canvases` refuses: a wrong
        canvas silently publishes a portrait capture on a 16:9 shape with
        nothing to show for it but a very small phone.
        """
        canvases = self.resolve_canvases(fallback or FrameStyle())
        if name not in canvases:
            raise ValueError(
                f"canvas {name!r} is not defined in this manifest; "
                "defined: " + ", ".join(sorted(canvases))
            )
        return canvases[name]

    def resolve_canvases(self, fallback: FrameStyle) -> dict[str, FrameStyle]:
        """Named canvases, defaulting to `fallback` when none are declared.

        Raises if an artifact names a canvas this manifest does not define.
        Refusing beats silently composing onto the wrong shape, which would
        publish a portrait capture on a 16:9 canvas with nothing to show for it
        but a very small phone.
        """
        canvases = dict(self.canvases) or {DEFAULT_CANVAS: fallback}
        wanted = {
            artifact.canvas for artifact in self.artifacts if isinstance(artifact, WindowArtifact)
        }
        if undefined := sorted(wanted - set(canvases)):
            raise ValueError(
                "artifacts name canvases this manifest does not define: " + ", ".join(undefined)
            )
        return canvases
