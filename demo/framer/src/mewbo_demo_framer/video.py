"""Framing a screen recording on the same canvas the still compositor uses.

An operator tool, invoked by hand. It is deliberately NOT part of
`make demo-frame`: the videos it frames are hand recordings that nothing in
this repo regenerates, so they are artifacts rather than sources and they never
enter `img-src` or `artifacts.json` (this package's `CLAUDE.md` owns that rule).
The manifest is still read here — for its wallpaper and its named canvases —
because those ARE shared, and a second canvas registry would be the drift this
package exists to prevent.

No geometry is re-derived. `VideoFramer` renders one canvas-sized RGBA cover
with Pillow — backdrop, drop shadow, and a rounded hole where the window goes —
off the very same `FrameStyle` a still reads, then hands ffmpeg one filter
graph that pads the scaled video to canvas size and paints that cover over it.
A framed video therefore matches a framed still pixel-for-pixel in padding,
radius and shadow, because both come from one set of measured ratios.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageChops
from pydantic import BaseModel, ConfigDict, Field

from .frame import FrameStyle, WindowFramer
from .manifest import DEFAULT_CANVAS, FrameManifest

PORTRAIT_CANVAS = "portrait"
"""The 4:3 canvas a source taller than it is wide is measured onto."""

_DEFAULT_MANIFEST = Path(__file__).resolve().parents[2] / "artifacts.json"


class VideoFramer(BaseModel):
    """Composites one video onto a named canvas from the frame manifest.

    Collaborators are injected as fields: the manifest supplies the wallpaper
    and every canvas style, and both binaries are named rather than assumed, so
    a test can point them somewhere else without patching a module global.

    Cost class: O(video duration) — one ffmpeg pass over every frame, plus one
    O(canvas area) Pillow render of the cover, paid once per video.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    manifest_path: Path
    """The frame manifest, read for its wallpaper and its named canvases. The
    same file the still pipeline uses; this tool declares no canvas of its own."""

    canvas: str | None = None
    """Which named canvas to compose onto, or `None` to measure the source. An
    explicit name always wins, and one the manifest does not define is refused
    rather than defaulted."""

    zoom: float = Field(default=1.0, gt=0.0, le=4.0)
    """Scales the placed window about the canvas centre, after the contain fit.

    Video-only on purpose. The still pipeline's committed bytes are a zero-diff
    gate, so a knob no artifact sets does not belong in `FrameStyle`; here every
    invocation is a person choosing, once, how much of the frame a phone should
    fill.
    """

    crf: int = Field(default=24, ge=0, le=51)
    """libx264 quality. Lower is larger; 24 is what keeps these small enough to
    commit."""

    fps: int = Field(default=30, ge=1, le=240)

    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"

    def measure(self, source: Path) -> tuple[int, int]:
        """The source's DISPLAY dimensions, rotation metadata applied.

        A phone recording routinely stores a transposed frame plus a quarter-turn
        display matrix, so the `width`/`height` ffprobe prints first are not what
        a player shows. ffmpeg's decoder auto-rotates before the filter graph, so
        every number downstream of here — the canvas choice, the contain fit, the
        `scale` target — has to be the display pair or the window comes out
        squashed onto a sideways aspect.
        """
        probe = json.loads(
            self._run(
                [
                    self.ffprobe,
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_streams",
                    "-of",
                    "json",
                    str(source),
                ]
            )
        )
        streams = probe.get("streams") or []
        if not streams:
            raise ValueError(f"no video stream in {source}")

        stream = streams[0]
        width, height = int(stream["width"]), int(stream["height"])
        # Both spellings: the legacy container tag and the modern side-data
        # entry. A file can carry either, and neither is more authoritative.
        rotation = float((stream.get("tags") or {}).get("rotate", 0))
        for side_data in stream.get("side_data_list") or []:
            rotation = float(side_data.get("rotation", rotation))
        if round(abs(rotation)) % 180 == 90:
            return height, width
        return width, height

    def canvas_for(self, size: tuple[int, int]) -> str:
        """The canvas a source of `size` belongs on when the caller named none.

        Taller than wide is a phone, which is what the 4:3 canvas exists for;
        everything else is a landscape capture and takes the 16:9 default, the
        same one a manifest artifact takes. Measuring is safe HERE and is not
        safe for an artifact: a manifest entry is a lasting classification that
        must not move when a capture viewport changes, whereas this is one
        operator framing one file, watching the result.
        """
        width, height = size
        return PORTRAIT_CANVAS if height > width else DEFAULT_CANVAS

    def place(self, style: FrameStyle, size: tuple[int, int]) -> tuple[int, int, int, int]:
        """The window rect: `style`'s own contain fit, scaled by `zoom` about the centre.

        Refuses a rect that leaves the canvas rather than clamping it, because
        `overlay` crops silently — the result would be a published video with a
        slice of the phone missing and nothing anywhere saying so.
        """
        x, y, width, height = style.place(size)
        if self.zoom == 1.0:
            return x, y, width, height

        width = max(1, round(width * self.zoom))
        height = max(1, round(height * self.zoom))
        if width > style.canvas_width or height > style.canvas_height:
            raise ValueError(
                f"zoom {self.zoom} places a {width}x{height} window on a "
                f"{style.canvas_width}x{style.canvas_height} canvas, which would crop it"
            )
        return (style.canvas_width - width) // 2, (style.canvas_height - height) // 2, width, height

    def cover(self, framer: WindowFramer, placement: tuple[int, int, int, int]) -> Image.Image:
        """The canvas as one RGBA layer: backdrop and shadow, transparent at the window.

        This is `WindowFramer.frame()` inverted. A still pastes its window ONTO
        the plate through the rounded mask; here the same plate is painted OVER
        a moving one through the same mask complemented. The blend is identical
        either way — `plate*(1-a) + window*a` — and doing it in this direction
        is what keeps ffmpeg to one overlay of one still image, with no alpha
        extraction filter in the graph.
        """
        x, y, width, height = placement
        alpha = Image.new("L", framer.style.canvas_size, 255)
        alpha.paste(ImageChops.invert(framer.window_mask((width, height))), (x, y))

        plate = framer.plate(placement).convert("RGBA")
        plate.putalpha(alpha)
        return plate

    def render(self, source: Path, output: Path) -> str:
        """Frame `source` onto its canvas, encode to `output`, return a report line.

        Encodes to a sibling of `output` and moves it into place only on
        success, so `output` may BE `source`: framing in place is the normal
        case here, since the docs reference these files by name and a rename
        would break every embed.
        """
        manifest = FrameManifest.load(self.manifest_path)
        size = self.measure(source)
        name = self.canvas or self.canvas_for(size)
        style = manifest.canvas(name)
        placement = self.place(style, size)

        wallpaper = (self.manifest_path.parent / manifest.wallpaper).resolve()
        framer = WindowFramer.load(wallpaper, style)

        output.parent.mkdir(parents=True, exist_ok=True)
        staged = output.with_name(f"{output.stem}.framing{output.suffix}")
        try:
            with tempfile.TemporaryDirectory() as scratch:
                cover_path = Path(scratch) / "cover.png"
                self.cover(framer, placement).save(cover_path, format="PNG")
                self._run(self._encode_command(source, cover_path, staged, style, placement))
            os.replace(staged, output)
        finally:
            staged.unlink(missing_ok=True)

        x, y, width, height = placement
        return (
            f"video/{name:<8} {output.name}  {style.canvas_width}x{style.canvas_height}"
            f"  window {width}x{height} at {x},{y}"
        )

    def _encode_command(
        self,
        source: Path,
        cover: Path,
        target: Path,
        style: FrameStyle,
        placement: tuple[int, int, int, int],
    ) -> list[str]:
        """The single ffmpeg invocation: contain the video, then paint the cover over it.

        `pad` puts the scaled window at the placement the style computed, and
        the looped cover supplies every other pixel, so the canvas is assembled
        in one pass with no intermediate file. `format=rgb` on the overlay is
        load-bearing: the cover's corner pixels carry fractional alpha, and
        blending those in a subsampled chroma space would fringe exactly the
        rounded corners this frame is for.
        """
        x, y, width, height = placement
        graph = (
            f"[0:v]scale={width}:{height}:flags=lanczos,setsar=1,"
            f"pad={style.canvas_width}:{style.canvas_height}:{x}:{y}[bg];"
            f"[bg][1:v]overlay=0:0:format=rgb:shortest=1,format=yuv420p[out]"
        )
        return [
            self.ffmpeg,
            "-v",
            "error",
            "-y",
            "-i",
            str(source),
            "-loop",
            "1",
            "-i",
            str(cover),
            "-filter_complex",
            graph,
            "-map",
            "[out]",
            # Optional: a screen recording may carry no audio at all, and a
            # missing stream must not fail the render.
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-crf",
            str(self.crf),
            "-r",
            str(self.fps),
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            "-c:a",
            "copy",
            "-shortest",
            str(target),
        ]

    def _run(self, command: list[str]) -> str:
        """Run `command`, returning its stdout and raising with its stderr on failure."""
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise RuntimeError(
                f"{Path(command[0]).name} failed ({result.returncode}): {result.stderr.strip()}"
            )
        return result.stdout


def main(argv: list[str] | None = None) -> int:
    """Frame one video onto a canvas from the manifest, printing one report line."""
    parser = argparse.ArgumentParser(prog="mewbo-demo-frame-video", description=__doc__)
    parser.add_argument("source", type=Path, help="the video to frame")
    parser.add_argument(
        "output", type=Path, help="where to write it; pass the source to frame it in place"
    )
    parser.add_argument(
        "--canvas",
        default=None,
        help=(
            "canvas name from the manifest; the default measures the source — "
            f"{PORTRAIT_CANVAS!r} when it is taller than wide, else {DEFAULT_CANVAS!r}"
        ),
    )
    parser.add_argument(
        "--zoom",
        type=float,
        default=1.0,
        help="scale the window about the canvas centre, after the contain fit",
    )
    parser.add_argument("--crf", type=int, default=24, help="libx264 quality; lower is larger")
    parser.add_argument("--fps", type=int, default=30, help="output frame rate")
    parser.add_argument(
        "--manifest",
        type=Path,
        default=_DEFAULT_MANIFEST,
        help="manifest supplying the wallpaper and the named canvases",
    )
    args = parser.parse_args(argv)

    framer = VideoFramer(
        manifest_path=args.manifest,
        canvas=args.canvas,
        zoom=args.zoom,
        crf=args.crf,
        fps=args.fps,
    )
    try:
        print(framer.render(args.source, args.output))
    except (FileNotFoundError, ValueError, RuntimeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
