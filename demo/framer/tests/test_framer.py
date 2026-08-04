"""Unit tests for `mewbo_demo_framer`: geometry, manifest validation, the runner's audit.

Sources are tiny synthetic images built with Pillow rather than the real
multi-megabyte captures, except for the one test that walks the real manifest
against the real `docs/assets/img-src` tree. Everything is driven from a
caller's position — `FrameRunner.run()` for the render tests, `FrameStyle` and
`FrameManifest` directly for the pure geometry and validation tests — stubbing
only the filesystem fixtures each test needs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from mewbo_demo_framer.frame import FrameStyle, SolidBackdrop, WallpaperBackdrop
from mewbo_demo_framer.manifest import FrameManifest, PassthroughArtifact, WindowArtifact
from mewbo_demo_framer.runner import FrameRunner
from PIL import Image
from pydantic import ValidationError

_REPO_ROOT = Path(__file__).resolve().parents[3]
_ARTIFACTS_JSON = _REPO_ROOT / "demo" / "framer" / "artifacts.json"
_IMG_SRC_DIR = _REPO_ROOT / "docs" / "assets" / "img-src"


def _solid(size: tuple[int, int], color: tuple[int, int, int] = (200, 60, 60)) -> Image.Image:
    """Build a tiny solid-colour image standing in for a real capture."""
    return Image.new("RGB", size, color)


def _save(image: Image.Image, path: Path) -> Path:
    """Write `image` to `path`, inferring the encoder from the suffix."""
    image.save(path)
    return path


def _write_manifest(
    path: Path, wallpaper: str, artifacts: list[dict], canvases: dict | None = None
) -> None:
    """Write a manifest JSON file directly, without going through `FrameManifest`."""
    body: dict = {"wallpaper": wallpaper, "artifacts": artifacts}
    if canvases is not None:
        body["canvases"] = canvases
    path.write_text(json.dumps(body), encoding="utf-8")


@pytest.fixture
def wallpaper_file(tmp_path: Path) -> Path:
    """A tiny synthetic wallpaper standing in for a real backdrop capture."""
    return _save(_solid((60, 40), color=(30, 60, 120)), tmp_path / "wallpaper.jpg")


# ── FrameStyle geometry: place() and content_box() are pure arithmetic ─────


@pytest.fixture
def small_style() -> FrameStyle:
    """A FrameStyle with round numbers, for placement arithmetic checked by hand."""
    return FrameStyle(
        canvas_width=1000, canvas_height=500, padding_y_ratio=0.1, padding_x_min_ratio=0.1
    )


def test_content_box_subtracts_twice_the_padding(small_style: FrameStyle) -> None:
    """content_box() shrinks each dimension by the padding on both sides."""
    assert small_style.content_box() == (800, 400)


def test_place_landscape_source_is_width_bound_and_centred(small_style: FrameStyle) -> None:
    """A source much wider than tall is scaled to the content box's width."""
    assert small_style.place((1600, 400)) == (100, 150, 800, 200)


def test_place_portrait_source_is_height_bound_and_centred(small_style: FrameStyle) -> None:
    """A source much taller than wide is scaled to the content box's height."""
    assert small_style.place((300, 800)) == (425, 50, 150, 400)


def test_place_square_source_is_centred_on_the_tighter_dimension(small_style: FrameStyle) -> None:
    """A square source is bound by whichever content-box dimension is tighter."""
    assert small_style.place((500, 500)) == (300, 50, 400, 400)


@pytest.mark.parametrize(
    "source_size", [(1600, 400), (300, 800), (500, 500), (800, 400), (1, 1), (4000, 1)]
)
def test_place_never_exceeds_the_content_box(
    small_style: FrameStyle, source_size: tuple[int, int]
) -> None:
    """No placement, at any source aspect ratio, exceeds the content box."""
    box_w, box_h = small_style.content_box()
    _, _, width, height = small_style.place(source_size)
    assert width <= box_w
    assert height <= box_h


def test_place_very_wide_source_is_contained_not_cropped() -> None:
    """A source far wider than the canvas is scaled to fit, never cropped.

    This is the `padding_x_min_ratio` floor: a source this wide is width-bound,
    so the placed width hits the content box's width exactly rather than the
    side margin shrinking below the configured floor.
    """
    style = FrameStyle()
    box_w, box_h = style.content_box()
    x, y, width, height = style.place((4000, 200))
    assert width == box_w
    assert height <= box_h
    assert x >= 0
    assert y >= 0
    assert x + width <= style.canvas_width
    assert y + height <= style.canvas_height


def test_style_validator_refuses_padding_that_eats_the_canvas() -> None:
    """A padding pair leaving no content box is a clean ValidationError, not a crash."""
    with pytest.raises(ValidationError):
        FrameStyle(
            padding_y_ratio=0.49, padding_x_min_ratio=0.49, canvas_width=20, canvas_height=20
        )


# ── The backdrop union: wallpaper by default, a validated solid on request ──


def test_frame_style_defaults_to_the_wallpaper_backdrop() -> None:
    """A style declaring no backdrop gets the wallpaper, which is the pre-existing behaviour."""
    assert FrameStyle().backdrop == WallpaperBackdrop()


@pytest.mark.parametrize(
    "color", ["#161513", "#000000", "#FFFFFF", "#AbCdEf", "#0d0d0b", "#201f1d"]
)
def test_solid_backdrop_accepts_a_six_digit_hex_colour(color: str) -> None:
    """Any `#rrggbb` string is a valid matte, in either case."""
    assert SolidBackdrop(color=color).color == color


@pytest.mark.parametrize(
    "color",
    [
        "161513",  # no leading hash
        "#16151",  # five digits
        "#1615133",  # seven digits
        "#gggggg",  # not hexadecimal
        "#abc",  # the three-digit CSS shorthand is deliberately not supported
        "rebeccapurple",  # a CSS colour name
        "rgb(22, 21, 19)",  # a CSS function
        "",
    ],
)
def test_solid_backdrop_rejects_a_malformed_colour(color: str) -> None:
    """A colour that is not `#rrggbb` is a ValidationError at definition.

    The point is WHERE this fails: a malformed matte in `artifacts.json` must
    surface as a manifest validation error before anything renders, not as a
    Pillow error partway through a run with some artifacts already published.
    """
    with pytest.raises(ValidationError):
        SolidBackdrop(color=color)


def test_solid_backdrop_parses_its_colour_to_an_rgb_triple() -> None:
    """`rgb()` splits the hex into the three 8-bit channels, case-insensitively."""
    assert SolidBackdrop(color="#161513").rgb() == (22, 21, 19)
    assert SolidBackdrop(color="#AbCdEf").rgb() == (171, 205, 239)


def test_solid_backdrop_forbids_an_unknown_field() -> None:
    """SolidBackdrop rejects a key it does not declare."""
    with pytest.raises(ValidationError):
        SolidBackdrop.model_validate({"color": "#161513", "bogus_field": 1})


def test_wallpaper_backdrop_forbids_an_unknown_field() -> None:
    """WallpaperBackdrop rejects a key it does not declare."""
    with pytest.raises(ValidationError):
        WallpaperBackdrop.model_validate({"bogus_field": 1})


def test_frame_style_parses_both_backdrop_kinds_into_their_own_classes() -> None:
    """The `kind` discriminator selects the matching concrete backdrop class."""
    assert isinstance(FrameStyle.model_validate({}).backdrop, WallpaperBackdrop)
    assert isinstance(
        FrameStyle.model_validate({"backdrop": {"kind": "wallpaper"}}).backdrop,
        WallpaperBackdrop,
    )
    solid = FrameStyle.model_validate({"backdrop": {"kind": "solid", "color": "#161513"}}).backdrop
    assert isinstance(solid, SolidBackdrop)
    assert solid.rgb() == (22, 21, 19)


def test_frame_style_rejects_an_unknown_backdrop_kind() -> None:
    """A `kind` outside the union's two literals is a ValidationError."""
    with pytest.raises(ValidationError):
        FrameStyle.model_validate({"backdrop": {"kind": "gradient", "color": "#161513"}})


def test_frame_style_rejects_a_solid_backdrop_with_no_colour() -> None:
    """`color` is required on the solid variant; there is no default matte."""
    with pytest.raises(ValidationError):
        FrameStyle.model_validate({"backdrop": {"kind": "solid"}})


# ── extra="forbid" at every trust boundary ──────────────────────────────────


def test_frame_style_forbids_an_unknown_field() -> None:
    """FrameStyle rejects a key it does not declare."""
    with pytest.raises(ValidationError):
        FrameStyle(bogus_field=1)


def test_frame_manifest_forbids_an_unknown_field() -> None:
    """FrameManifest rejects a top-level key it does not declare."""
    with pytest.raises(ValidationError):
        FrameManifest.model_validate(
            {
                "wallpaper": "wallpaper.jpg",
                "artifacts": [{"treatment": "passthrough", "file": "a.png"}],
                "bogus_field": 1,
            }
        )


def test_window_artifact_forbids_an_unknown_field() -> None:
    """WindowArtifact rejects a key it does not declare."""
    with pytest.raises(ValidationError):
        WindowArtifact.model_validate({"file": "a.png", "bogus_field": 1})


def test_passthrough_artifact_forbids_an_unknown_field() -> None:
    """PassthroughArtifact rejects a key it does not declare."""
    with pytest.raises(ValidationError):
        PassthroughArtifact.model_validate({"file": "a.png", "bogus_field": 1})


# ── The discriminated union ─────────────────────────────────────────────────


def test_manifest_parses_window_and_passthrough_into_their_own_classes() -> None:
    """One manifest with both treatments yields the matching concrete classes."""
    manifest = FrameManifest.model_validate(
        {
            "wallpaper": "wallpaper.jpg",
            "artifacts": [
                {"treatment": "window", "file": "a.png"},
                {"treatment": "passthrough", "file": "b.gif"},
            ],
        }
    )
    window, passthrough = manifest.artifacts
    assert isinstance(window, WindowArtifact)
    assert isinstance(passthrough, PassthroughArtifact)
    assert manifest.declared_files() == {"a.png", "b.gif"}


def test_manifest_rejects_an_unknown_treatment() -> None:
    """A `treatment` outside the union's two literals is a ValidationError."""
    with pytest.raises(ValidationError):
        FrameManifest.model_validate(
            {
                "wallpaper": "wallpaper.jpg",
                "artifacts": [{"treatment": "carousel", "file": "a.png"}],
            }
        )


# ── FrameRunner._audit: the guard against a silently unclassified capture ──


def test_audit_rejects_a_source_file_with_no_manifest_entry(tmp_path: Path) -> None:
    """A capture with no manifest entry fails loudly, naming the file."""
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    _save(_solid((40, 30)), source_dir / "declared.png")
    _save(_solid((40, 30)), source_dir / "mystery.png")
    manifest = FrameManifest.model_validate(
        {
            "wallpaper": "wallpaper.jpg",
            "artifacts": [{"treatment": "passthrough", "file": "declared.png"}],
        }
    )
    runner = FrameRunner(
        manifest_path=tmp_path / "artifacts.json",
        source_dir=source_dir,
        output_dir=tmp_path / "out",
    )
    with pytest.raises(ValueError, match="mystery.png"):
        runner._audit(manifest)


def test_audit_rejects_a_manifest_entry_with_no_source_file(tmp_path: Path) -> None:
    """A manifest entry with no backing file fails loudly, naming the file."""
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    manifest = FrameManifest.model_validate(
        {
            "wallpaper": "wallpaper.jpg",
            "artifacts": [{"treatment": "window", "file": "ghost.png"}],
        }
    )
    runner = FrameRunner(
        manifest_path=tmp_path / "artifacts.json",
        source_dir=source_dir,
        output_dir=tmp_path / "out",
    )
    with pytest.raises(FileNotFoundError, match="ghost.png"):
        runner._audit(manifest)


# ── FrameRunner.run(): passthrough is a copy, window is a composite ────────


def test_passthrough_output_is_byte_identical_to_its_source(
    tmp_path: Path, wallpaper_file: Path
) -> None:
    """A passthrough artifact is published with zero bytes changed."""
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    source_path = _save(_solid((40, 30), color=(10, 200, 90)), source_dir / "raw.png")

    manifest_path = tmp_path / "artifacts.json"
    _write_manifest(
        manifest_path, wallpaper_file.name, [{"treatment": "passthrough", "file": "raw.png"}]
    )
    output_dir = tmp_path / "out"
    FrameRunner(manifest_path=manifest_path, source_dir=source_dir, output_dir=output_dir).run()

    assert (output_dir / "raw.png").read_bytes() == source_path.read_bytes()


def test_window_output_is_canvas_sized_and_differs_from_its_source(
    tmp_path: Path, wallpaper_file: Path
) -> None:
    """A window artifact is exactly canvas-sized and is not a copy of its source."""
    style = FrameStyle(canvas_width=480, canvas_height=270)
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    source_path = _save(_solid((320, 200), color=(200, 50, 50)), source_dir / "shot.png")

    manifest_path = tmp_path / "artifacts.json"
    _write_manifest(
        manifest_path, wallpaper_file.name, [{"treatment": "window", "file": "shot.png"}]
    )
    output_dir = tmp_path / "out"
    FrameRunner(
        manifest_path=manifest_path, source_dir=source_dir, output_dir=output_dir, style=style
    ).run()

    output_path = output_dir / "shot.png"
    with Image.open(output_path) as framed:
        assert framed.size == (480, 270)
    assert output_path.read_bytes() != source_path.read_bytes()


def test_solid_canvas_fills_the_matte_and_never_the_wallpaper(
    tmp_path: Path, wallpaper_file: Path
) -> None:
    """A window on a solid canvas is canvas-sized and matted in the declared colour.

    The wallpaper fixture is a distinct blue, so asserting the corners are the
    declared matte proves the wallpaper reached no pixel of this canvas — not
    merely that something dark was painted over it.
    """
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    _save(_solid((320, 200), color=(200, 50, 50)), source_dir / "crop.png")

    manifest_path = tmp_path / "artifacts.json"
    _write_manifest(
        manifest_path,
        wallpaper_file.name,
        [{"treatment": "window", "file": "crop.png", "canvas": "closeup"}],
        canvases={
            "closeup": {
                "canvas_width": 480,
                "canvas_height": 360,
                "shadow_opacity": 0.0,
                "backdrop": {"kind": "solid", "color": "#161513"},
            }
        },
    )
    output_dir = tmp_path / "out"
    FrameRunner(manifest_path=manifest_path, source_dir=source_dir, output_dir=output_dir).run()

    with Image.open(output_dir / "crop.png") as framed:
        rgb = framed.convert("RGB")
        assert rgb.size == (480, 360)
        corners = [(0, 0), (479, 0), (0, 359), (479, 359)]
        assert [rgb.getpixel(point) for point in corners] == [(22, 21, 19)] * 4
        assert rgb.getpixel((240, 180)) == (200, 50, 50)


def test_declaring_the_wallpaper_backdrop_renders_exactly_the_default(
    tmp_path: Path, wallpaper_file: Path
) -> None:
    """Spelling the default backdrop out changes not one byte.

    This is the guard on the canvases that predate the field: adding
    `backdrop` to `FrameStyle` must leave every existing artifact's bytes
    alone, and the two renders here differ only in whether the default was
    written down.
    """
    geometry = {"canvas_width": 480, "canvas_height": 270}
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    _save(_solid((320, 200), color=(80, 140, 200)), source_dir / "shot.png")

    outputs = []
    for index, canvas in enumerate((geometry, {**geometry, "backdrop": {"kind": "wallpaper"}})):
        manifest_path = tmp_path / f"artifacts_{index}.json"
        _write_manifest(
            manifest_path,
            wallpaper_file.name,
            [{"treatment": "window", "file": "shot.png"}],
            canvases={"wide": canvas},
        )
        output_dir = tmp_path / f"out_{index}"
        FrameRunner(
            manifest_path=manifest_path, source_dir=source_dir, output_dir=output_dir
        ).run()
        outputs.append((output_dir / "shot.png").read_bytes())

    assert outputs[0] == outputs[1]


def test_render_is_deterministic_across_independent_runs(
    tmp_path: Path, wallpaper_file: Path
) -> None:
    """Rendering the same manifest twice produces byte-identical output.

    This pins the property the demo pipeline's zero-diff screenshot gate
    depends on: re-running the pipeline with no source change must not move a
    single committed byte. Every treatment and every backdrop is covered in one
    manifest, because a run publishes them together.
    """
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    _save(_solid((320, 200), color=(80, 140, 200)), source_dir / "window.png")
    _save(_solid((200, 300), color=(60, 180, 90)), source_dir / "crop.png")
    _save(_solid((100, 60), color=(10, 10, 10)), source_dir / "flat.jpg")

    manifest_path = tmp_path / "artifacts.json"
    _write_manifest(
        manifest_path,
        wallpaper_file.name,
        [
            {"treatment": "window", "file": "window.png"},
            {"treatment": "window", "file": "crop.png", "canvas": "closeup"},
            {"treatment": "passthrough", "file": "flat.jpg"},
        ],
        canvases={
            "wide": {"canvas_width": 480, "canvas_height": 270},
            "closeup": {
                "canvas_width": 360,
                "canvas_height": 270,
                "shadow_opacity": 0.0,
                "backdrop": {"kind": "solid", "color": "#161513"},
            },
        },
    )

    out_a, out_b = tmp_path / "out_a", tmp_path / "out_b"
    common = {"manifest_path": manifest_path, "source_dir": source_dir}
    FrameRunner(output_dir=out_a, **common).run()
    FrameRunner(output_dir=out_b, **common).run()

    for name in ("window.png", "crop.png", "flat.jpg"):
        assert (out_a / name).read_bytes() == (out_b / name).read_bytes()


# ── The committed manifest itself ───────────────────────────────────────────


def test_real_manifest_is_valid_and_exactly_covers_the_source_tree(tmp_path: Path) -> None:
    """The committed manifest is well-formed and its declared files match the source tree.

    This is the test that catches a new capture landing in `docs/assets/img-src`
    without anyone classifying it as `window` or `passthrough` in `artifacts.json`
    — `FrameRunner._audit` raises on any drift in either direction, so a clean
    return is the assertion.
    """
    manifest = FrameManifest.load(_ARTIFACTS_JSON)
    runner = FrameRunner(
        manifest_path=_ARTIFACTS_JSON, source_dir=_IMG_SRC_DIR, output_dir=tmp_path
    )
    runner._audit(manifest)
