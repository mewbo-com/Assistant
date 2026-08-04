"""The window-on-backdrop composite: pure geometry and pure pixels.

Nothing here touches the filesystem beyond one explicit loader. `FrameStyle`
turns canvas dimensions into placement numbers; `WindowFramer` turns those
numbers plus two images into one image. Both are injectable, so a test can
assert geometry without decoding a JPEG.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from PIL import Image, ImageChops, ImageDraw, ImageFilter
from pydantic import BaseModel, ConfigDict, Field, model_validator

# Pillow >= 10 moved the resampling filters onto an enum; the module-level
# aliases are deprecated and this package is new enough to skip them.
_LANCZOS = Image.Resampling.LANCZOS

_HEX_COLOR = r"^#[0-9a-fA-F]{6}$"


class WallpaperBackdrop(BaseModel):
    """Fill the canvas with a photographic wallpaper, cover-fitted.

    Cost class: O(canvas area), paid once per canvas by `WindowFramer.load`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["wallpaper"] = "wallpaper"

    def prepare(self, wallpaper: Image.Image, size: tuple[int, int]) -> Image.Image:
        """Scale `wallpaper` to fill `size` preserving aspect, centre-cropping the excess."""
        target_w, target_h = size
        src_w, src_h = wallpaper.size
        scale = max(target_w / src_w, target_h / src_h)
        scaled = wallpaper.resize((round(src_w * scale), round(src_h * scale)), _LANCZOS)
        left = (scaled.width - target_w) // 2
        top = (scaled.height - target_h) // 2
        return scaled.crop((left, top, left + target_w, top + target_h))


class SolidBackdrop(BaseModel):
    """Fill the canvas with one flat colour.

    For a source that is an element crop rather than a window: a card lifted
    out of the app reads as a card, so it belongs on the surface it was lifted
    off, not on a desktop photograph it never sat on.

    Cost class: O(canvas area), paid once per canvas by `WindowFramer.load`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["solid"] = "solid"

    color: str = Field(pattern=_HEX_COLOR)
    """The matte, as `#rrggbb`. Validated at definition, because a malformed
    colour must be a `ValidationError` naming the manifest rather than a
    `ValueError` from deep inside Pillow halfway through a render."""

    def prepare(self, wallpaper: Image.Image, size: tuple[int, int]) -> Image.Image:
        """Fill `size` with `color`, ignoring the wallpaper this canvas does not use.

        The unused argument is the point: both backdrops answer the same call,
        so `load` prepares whichever one the style declares without a branch.
        """
        return Image.new("RGB", size, self.rgb())

    def rgb(self) -> tuple[int, int, int]:
        """`color` as an 8-bit RGB triple."""
        return (
            int(self.color[1:3], 16),
            int(self.color[3:5], 16),
            int(self.color[5:7], 16),
        )


Backdrop = Annotated[
    WallpaperBackdrop | SolidBackdrop,
    Field(discriminator="kind"),
]


class FrameStyle(BaseModel):
    """Every visual parameter of the frame, plus the geometry it implies.

    Ratios rather than pixels throughout, so one style renders identically at
    any canvas size. The defaults are measured off the hand-composited
    references the owner produced before this package existed
    (`mewbo-apps-01-detail.png` and siblings): a 2400x1350 canvas, a window
    inset about 5.1% vertically and centred, a 25px corner radius, and a large
    soft shadow displaced slightly downward.

    Cost class: O(1). Every method is arithmetic over the canvas dimensions.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    canvas_width: int = Field(default=2400, ge=16)
    canvas_height: int = Field(default=1350, ge=9)

    backdrop: Backdrop = WallpaperBackdrop()
    """What fills the canvas behind the window. The wallpaper is the default
    and stays the default: every canvas that predates this field renders
    byte-identically without declaring one."""

    padding_y_ratio: float = Field(default=0.051, ge=0.0, lt=0.5)
    """Vertical breathing room, as a fraction of canvas height."""

    padding_x_min_ratio: float = Field(default=0.060, ge=0.0, lt=0.5)
    """Horizontal floor. It only binds for a source wider than the padded box;
    for anything narrower the side margin falls out of the contain fit."""

    radius_ratio: float = Field(default=0.0104, ge=0.0, le=0.25)
    shadow_blur_ratio: float = Field(default=0.0292, ge=0.0, le=0.25)
    shadow_spread_ratio: float = Field(default=0.0042, ge=0.0, le=0.25)
    shadow_offset_y_ratio: float = Field(default=0.0100, ge=-0.25, le=0.25)
    shadow_opacity: float = Field(default=0.55, ge=0.0, le=1.0)

    ambient_blur_scale: float = Field(default=0.5, ge=0.0, le=2.0)
    """A second, tighter shadow at zero offset. One cast shadow alone reads as
    a sticker hovering over the wallpaper; the contact layer seats it."""

    ambient_opacity_scale: float = Field(default=0.35, ge=0.0, le=2.0)

    supersample: int = Field(default=4, ge=1, le=8)
    """Corner masks are drawn at this multiple and downsampled, because
    Pillow's `rounded_rectangle` is aliased and a corner staircase is the most
    visible tell that a composite was not done by a browser."""

    @model_validator(mode="after")
    def _padding_must_leave_a_content_box(self) -> FrameStyle:
        """Reject a style whose padding consumes the whole canvas."""
        box_w, box_h = self.content_box()
        if box_w < 1 or box_h < 1:
            raise ValueError(
                f"padding leaves no content box: {box_w}x{box_h} on a "
                f"{self.canvas_width}x{self.canvas_height} canvas"
            )
        return self

    @property
    def canvas_size(self) -> tuple[int, int]:
        """The output raster size."""
        return self.canvas_width, self.canvas_height

    def content_box(self) -> tuple[int, int]:
        """The box a source image is contained within, in pixels."""
        pad_x = round(self.canvas_width * self.padding_x_min_ratio)
        pad_y = round(self.canvas_height * self.padding_y_ratio)
        return self.canvas_width - 2 * pad_x, self.canvas_height - 2 * pad_y

    def place(self, source_size: tuple[int, int]) -> tuple[int, int, int, int]:
        """Contain `source_size` in the content box and centre it.

        Returns `(x, y, width, height)`. Aspect ratio is always preserved: a
        source is never cropped and never stretched, so a portrait capture
        simply shows more wallpaper at its sides than a landscape one does.
        """
        box_w, box_h = self.content_box()
        src_w, src_h = source_size
        scale = min(box_w / src_w, box_h / src_h)
        width = max(1, round(src_w * scale))
        height = max(1, round(src_h * scale))
        return (
            (self.canvas_width - width) // 2,
            (self.canvas_height - height) // 2,
            width,
            height,
        )

    def radius_px(self) -> int:
        """Corner radius of the composited window."""
        return round(self.canvas_width * self.radius_ratio)

    def shadow_blur_px(self) -> float:
        """Gaussian sigma of the cast shadow."""
        return self.canvas_width * self.shadow_blur_ratio

    def shadow_spread_px(self) -> int:
        """How far the shadow silhouette grows beyond the window itself."""
        return round(self.canvas_width * self.shadow_spread_ratio)

    def shadow_offset_y_px(self) -> int:
        """Downward displacement of the cast shadow."""
        return round(self.canvas_height * self.shadow_offset_y_ratio)


class WindowFramer(BaseModel):
    """Composites one source image onto one backdrop, per a `FrameStyle`.

    Collaborators are injected: the style and the already prepared backdrop are
    fields, so framing many artifacts reuses one prepared background instead of
    re-decoding and re-scaling per file.

    Cost class: O(canvas area) per `frame` call, independent of how many
    artifacts a run covers.
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    style: FrameStyle
    backdrop: Image.Image
    """Already prepared to `style.canvas_size` by `load`, per `style.backdrop`.
    Whether that meant cover-fitting a photograph or filling a colour is
    settled before `frame` runs, so the composite itself never branches."""

    @classmethod
    def load(cls, wallpaper_path: Path, style: FrameStyle | None = None) -> WindowFramer:
        """Build a framer, preparing its backdrop once up front.

        The wallpaper is decoded whatever the style declares, so a manifest is
        never silently freed of its wallpaper by moving one canvas to a solid
        colour: the path stays a real file that every canvas is checked
        against, and a missing one still fails on the first canvas built.
        """
        resolved = style or FrameStyle()
        with Image.open(wallpaper_path) as handle:
            wallpaper = handle.convert("RGB")
        return cls(
            style=resolved,
            backdrop=resolved.backdrop.prepare(wallpaper, resolved.canvas_size),
        )

    def window_mask(self, size: tuple[int, int]) -> Image.Image:
        """An anti-aliased rounded-rectangle alpha mask at `size`.

        Public because the window's silhouette is what a second compositor
        needs to reproduce this frame: the video path paints the same mask
        complemented, so both surfaces round their corners on one shape.
        """
        width, height = size
        factor = self.style.supersample
        big = Image.new("L", (width * factor, height * factor), 0)
        ImageDraw.Draw(big).rounded_rectangle(
            (0, 0, width * factor - 1, height * factor - 1),
            radius=self.style.radius_px() * factor,
            fill=255,
        )
        return big.resize((width, height), _LANCZOS)

    def _blurred_silhouette(
        self, silhouette: Image.Image, origin: tuple[int, int], sigma: float, alpha: float
    ) -> Image.Image:
        """Paste `silhouette` at `origin` on a canvas-sized mask, blur, scale alpha."""
        layer = Image.new("L", self.style.canvas_size, 0)
        layer.paste(silhouette, origin)
        blurred = layer.filter(ImageFilter.GaussianBlur(sigma))
        return blurred.point(lambda value: int(value * alpha))

    def _shadow_mask(self, placement: tuple[int, int, int, int]) -> Image.Image:
        """The combined cast and contact shadow, as one alpha mask.

        Both layers blur the window's own rounded silhouette rather than its
        bounding box. That is what `filter: drop-shadow()` does in a browser
        and what `box-shadow` does not: a box shadow leaks square corners out
        from behind a rounded window.
        """
        style = self.style
        x, y, width, height = placement
        spread = style.shadow_spread_px()
        silhouette = self.window_mask((width, height)).resize(
            (width + 2 * spread, height + 2 * spread), _LANCZOS
        )
        corner = (x - spread, y - spread)

        cast = self._blurred_silhouette(
            silhouette,
            (corner[0], corner[1] + style.shadow_offset_y_px()),
            style.shadow_blur_px(),
            style.shadow_opacity,
        )
        contact = self._blurred_silhouette(
            silhouette,
            corner,
            style.shadow_blur_px() * style.ambient_blur_scale,
            style.shadow_opacity * style.ambient_opacity_scale,
        )
        # `cast over contact`: cast + contact * (1 - cast), in 8-bit space.
        under = ImageChops.multiply(contact, ImageChops.invert(cast))
        return ImageChops.add(cast, under)

    def plate(self, placement: tuple[int, int, int, int]) -> Image.Image:
        """The backdrop with `placement`'s shadow composited, and no window yet.

        Everything a framed artifact contains except the artwork itself. A
        still pastes its window onto this; the video compositor paints it back
        over a moving one. Splitting it out is what lets both read the same
        measured geometry instead of one of them re-deriving it.
        """
        black = Image.new("RGB", self.style.canvas_size, (0, 0, 0))
        return Image.composite(black, self.backdrop, self._shadow_mask(placement))

    def frame(self, source: Image.Image) -> Image.Image:
        """Render `source` as a rounded window on the prepared backdrop.

        The source is contained, never cropped, so this is safe for any input
        aspect ratio; a portrait capture simply occupies less of the canvas.
        """
        placement = self.style.place(source.size)
        x, y, width, height = placement
        inner = source.convert("RGB").resize((width, height), _LANCZOS)

        canvas = self.plate(placement)
        canvas.paste(inner, (x, y), self.window_mask((width, height)))
        return canvas
