"""Unit tests for `mewbo_demo_framer.video`: measurement, canvas choice, and the cover.

Nothing here runs ffmpeg. The two subprocess seams are covered by pointing
`VideoFramer` at a stub executable instead, which is the reason both binaries
are injectable fields — the interesting behaviour is what the class does with a
probe result, not that ffprobe works.

The load-bearing test is `test_cover_composited_under_a_video_frame_equals_the_still`:
it pins that the video path reproduces the still path exactly, which is the whole
claim this module makes.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from mewbo_demo_framer.frame import FrameStyle, WindowFramer
from mewbo_demo_framer.manifest import FrameManifest
from mewbo_demo_framer.video import VideoFramer
from PIL import Image
from pydantic import ValidationError

_REPO_ROOT = Path(__file__).resolve().parents[3]
_ARTIFACTS_JSON = _REPO_ROOT / "demo" / "framer" / "artifacts.json"


@pytest.fixture
def wallpaper_file(tmp_path: Path) -> Path:
    """A tiny synthetic wallpaper standing in for a real backdrop capture."""
    path = tmp_path / "wallpaper.jpg"
    Image.new("RGB", (60, 40), (30, 60, 120)).save(path)
    return path


@pytest.fixture
def framer() -> VideoFramer:
    """A framer pointed at the committed manifest, with default encode settings."""
    return VideoFramer(manifest_path=_ARTIFACTS_JSON)


def _ffprobe_stub(tmp_path: Path, stream: dict) -> str:
    """Write an executable that prints `stream` as an ffprobe JSON payload."""
    payload = json.dumps({"streams": [stream]})
    script = tmp_path / "ffprobe-stub"
    script.write_text(f"#!/bin/sh\ncat <<'PAYLOAD'\n{payload}\nPAYLOAD\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return str(script)


# ── Canvas auto-detection: the flag the operator does not have to pass ──────


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        ((1440, 3120), "portrait"),  # the Aura chat recording
        ((1440, 2598), "portrait"),  # the Aura overlay recording
        ((1080, 1081), "portrait"),  # taller by one pixel is still taller
        ((2400, 1350), "wide"),  # a 16:9 web capture
        ((1920, 1080), "wide"),
        ((1920, 934), "wide"),  # the CLI recording, a 2.06:1 ultrawide
        ((800, 800), "wide"),  # square is not "taller than wide"
    ],
)
def test_canvas_for_picks_portrait_only_when_the_source_is_taller(
    framer: VideoFramer, size: tuple[int, int], expected: str
) -> None:
    """Taller than wide measures onto the 4:3 canvas; everything else onto 16:9."""
    assert framer.canvas_for(size) == expected


def test_both_auto_detected_canvases_exist_in_the_committed_manifest() -> None:
    """Auto-detection may only name canvases the shipped manifest actually defines.

    Measuring is worthless if the name it picks is one `canvas()` then refuses,
    so this is the guard on renaming a canvas in `artifacts.json` without
    touching the detector.
    """
    manifest = FrameManifest.load(_ARTIFACTS_JSON)
    framer = VideoFramer(manifest_path=_ARTIFACTS_JSON)
    for size in ((1440, 3120), (2400, 1350)):
        assert manifest.canvas(framer.canvas_for(size)).canvas_height == 1350


# ── FrameManifest.canvas: a name the manifest does not define is refused ────


def test_manifest_canvas_returns_the_named_style() -> None:
    """A defined name resolves to that canvas's own style, not the default one."""
    manifest = FrameManifest.load(_ARTIFACTS_JSON)
    assert manifest.canvas("portrait").canvas_size == (1800, 1350)
    assert manifest.canvas("wide").canvas_size == (2400, 1350)


def test_manifest_canvas_refuses_an_undefined_name_and_lists_what_it_has() -> None:
    """An unknown canvas is a ValueError naming the alternatives, never a silent default.

    Falling back to the wide canvas here would publish a phone recording at a
    quarter of the frame width and report success.
    """
    manifest = FrameManifest.load(_ARTIFACTS_JSON)
    with pytest.raises(ValueError, match="tall-boy") as error:
        manifest.canvas("tall-boy")
    assert "portrait" in str(error.value)


def test_manifest_canvas_refuses_a_name_absent_from_a_minimal_manifest() -> None:
    """A manifest declaring no canvases at all defines only the default name."""
    manifest = FrameManifest.model_validate(
        {"wallpaper": "w.jpg", "artifacts": [{"treatment": "passthrough", "file": "a.png"}]}
    )
    assert manifest.canvas("wide").canvas_size == FrameStyle().canvas_size
    with pytest.raises(ValueError, match="portrait"):
        manifest.canvas("portrait")


# ── Measurement: the display pair, not the coded one ────────────────────────


def test_measure_returns_the_coded_size_when_there_is_no_rotation(
    framer: VideoFramer, tmp_path: Path
) -> None:
    """An unrotated stream measures exactly as ffprobe reports it."""
    probing = framer.model_copy(
        update={"ffprobe": _ffprobe_stub(tmp_path, {"width": 1440, "height": 3120})}
    )
    assert probing.measure(tmp_path / "clip.mp4") == (1440, 3120)


@pytest.mark.parametrize("rotation", [-90, 90, 270, -270])
def test_measure_transposes_a_quarter_turn_display_matrix(
    framer: VideoFramer, tmp_path: Path, rotation: int
) -> None:
    """A quarter-turn rotation swaps the pair, because the player shows it swapped.

    Get this wrong and the contain fit is computed against a sideways aspect,
    so the window is placed landscape and the phone renders squashed inside it.
    """
    probing = framer.model_copy(
        update={
            "ffprobe": _ffprobe_stub(
                tmp_path,
                {
                    "width": 3120,
                    "height": 1440,
                    "side_data_list": [{"side_data_type": "Display Matrix", "rotation": rotation}],
                },
            )
        }
    )
    assert probing.measure(tmp_path / "clip.mp4") == (1440, 3120)


def test_measure_honours_the_legacy_rotate_tag(framer: VideoFramer, tmp_path: Path) -> None:
    """The older container tag spelling rotates too; a file may carry either."""
    stream = {"width": 3120, "height": 1440, "tags": {"rotate": "270"}}
    probing = framer.model_copy(update={"ffprobe": _ffprobe_stub(tmp_path, stream)})
    assert probing.measure(tmp_path / "clip.mp4") == (1440, 3120)


@pytest.mark.parametrize("rotation", [0, 180, -180])
def test_measure_leaves_a_half_turn_alone(
    framer: VideoFramer, tmp_path: Path, rotation: int
) -> None:
    """A half turn is upside down, not transposed, so the pair is unchanged."""
    probing = framer.model_copy(
        update={
            "ffprobe": _ffprobe_stub(
                tmp_path,
                {"width": 1440, "height": 3120, "side_data_list": [{"rotation": rotation}]},
            )
        }
    )
    assert probing.measure(tmp_path / "clip.mp4") == (1440, 3120)


def test_measure_refuses_a_file_with_no_video_stream(framer: VideoFramer, tmp_path: Path) -> None:
    """An audio-only or unreadable file fails naming the path, not with a KeyError."""
    script = tmp_path / "ffprobe-empty"
    script.write_text('#!/bin/sh\necho \'{"streams": []}\'\n', encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    probing = framer.model_copy(update={"ffprobe": str(script)})
    with pytest.raises(ValueError, match="clip.mp4"):
        probing.measure(tmp_path / "clip.mp4")


def test_run_raises_with_the_tool_s_own_stderr(framer: VideoFramer, tmp_path: Path) -> None:
    """A non-zero exit surfaces the binary's diagnostic rather than swallowing it."""
    script = tmp_path / "ffprobe-broken"
    script.write_text("#!/bin/sh\necho 'moov atom not found' >&2\nexit 3\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    probing = framer.model_copy(update={"ffprobe": str(script)})
    with pytest.raises(RuntimeError, match="moov atom not found"):
        probing.measure(tmp_path / "clip.mp4")


# ── place(): the style's own fit, and the zoom that may not crop ────────────


def test_place_with_no_zoom_is_exactly_the_style_s_own_fit(framer: VideoFramer) -> None:
    """At zoom 1 the video path adds nothing to the geometry the stills use.

    These are the real numbers for the Aura chat recording on the shipped
    portrait canvas, and they are also what the hand-composited still
    `mewbo-aura-01-chat.png` measures.
    """
    style = FrameManifest.load(_ARTIFACTS_JSON).canvas("portrait")
    assert framer.place(style, (1440, 3120)) == style.place((1440, 3120))
    assert framer.place(style, (1440, 3120)) == (620, 69, 559, 1212)


def test_an_ultrawide_source_underfills_the_wide_canvas_rather_than_being_cropped(
    framer: VideoFramer,
) -> None:
    """A 2.06:1 source lands width-bound on the 16:9 canvas, with wallpaper above and below.

    These are the real numbers for the CLI recording. It is wider than 16:9, so
    the contain fit binds on WIDTH — the window hits the horizontal padding
    floor exactly (144px each side) and leaves 161px of backdrop top and
    bottom. That under-fill is the correct answer, not a bug to special-case:
    the canvas is the constant and the source's own ratio is not, and `place()`
    contains rather than crops for exactly this reason.
    """
    style = FrameManifest.load(_ARTIFACTS_JSON).canvas("wide")
    x, y, width, height = framer.place(style, (1920, 934))

    assert (x, y, width, height) == (144, 161, 2112, 1027)
    assert x == round(style.canvas_width * style.padding_x_min_ratio)
    assert y > 0, "an ultrawide source must show backdrop above and below it"
    assert width / height == pytest.approx(1920 / 934, abs=0.002)


def test_zoom_grows_the_window_about_the_canvas_centre() -> None:
    """A zoom scales both dimensions and re-centres, leaving the aspect alone."""
    style = FrameStyle(canvas_width=1000, canvas_height=800, padding_y_ratio=0.1)
    framer = VideoFramer(manifest_path=_ARTIFACTS_JSON, zoom=1.2)
    x, y, width, height = style.place((300, 600))
    zx, zy, zwidth, zheight = framer.place(style, (300, 600))
    assert (zwidth, zheight) == (round(width * 1.2), round(height * 1.2))
    assert zx + zwidth // 2 == pytest.approx(x + width // 2, abs=1)
    assert zy + zheight // 2 == pytest.approx(y + height // 2, abs=1)


def test_zoom_that_would_leave_the_canvas_is_refused() -> None:
    """A window bigger than the canvas raises rather than being cropped by overlay.

    ffmpeg crops silently, so clamping or ignoring this would publish a video
    with a slice of the phone missing and report success.
    """
    style = FrameStyle(canvas_width=1800, canvas_height=1350)
    framer = VideoFramer(manifest_path=_ARTIFACTS_JSON, zoom=2.0)
    with pytest.raises(ValueError, match="crop"):
        framer.place(style, (1440, 3120))


@pytest.mark.parametrize("zoom", [0.0, -1.0, 4.5])
def test_zoom_outside_its_range_is_a_validation_error(zoom: float) -> None:
    """A nonsensical zoom fails at definition, before anything is decoded."""
    with pytest.raises(ValidationError):
        VideoFramer(manifest_path=_ARTIFACTS_JSON, zoom=zoom)


def test_video_framer_forbids_an_unknown_field() -> None:
    """VideoFramer rejects a key it does not declare."""
    with pytest.raises(ValidationError):
        VideoFramer(manifest_path=_ARTIFACTS_JSON, bogus_field=1)


# ── The cover: the still composite, inverted ────────────────────────────────


def test_cover_is_opaque_at_the_canvas_edge_and_clear_at_the_window_centre(
    wallpaper_file: Path,
) -> None:
    """The alpha channel is the window mask complemented: 0 inside, 255 outside."""
    style = FrameStyle(canvas_width=480, canvas_height=360)
    window = WindowFramer.load(wallpaper_file, style)
    framer = VideoFramer(manifest_path=_ARTIFACTS_JSON)
    placement = framer.place(style, (200, 400))

    alpha = framer.cover(window, placement).getchannel("A")
    assert alpha.getpixel((0, 0)) == 255
    assert alpha.getpixel((479, 359)) == 255
    assert alpha.getpixel((240, 180)) == 0


def test_cover_composited_under_a_video_frame_equals_the_still(wallpaper_file: Path) -> None:
    """Painting the cover over a placed frame reproduces `WindowFramer.frame()` byte for byte.

    This is the claim the whole module rests on: a framed video sits on the
    canvas exactly where a framed still does, with the same padding, the same
    radius and the same shadow, because the two share one `FrameStyle` and one
    mask rather than two sets of numbers that can drift apart.

    The composite here models the ffmpeg graph — `scale` and `pad` place the
    frame, `overlay` blends the cover over it — using Pillow's own 8-bit blend.
    It pins the Pillow half exactly; ffmpeg's rounding on antialiased corner
    pixels is its own, and is what the rendered artifact is checked against.
    """
    style = FrameStyle(canvas_width=480, canvas_height=360)
    window = WindowFramer.load(wallpaper_file, style)
    framer = VideoFramer(manifest_path=_ARTIFACTS_JSON)

    source = Image.new("RGB", (200, 400), (200, 50, 50))
    still = window.frame(source)

    x, y, width, height = framer.place(style, source.size)
    placed = Image.new("RGB", style.canvas_size, (0, 0, 0))
    placed.paste(source.resize((width, height), Image.Resampling.LANCZOS), (x, y))
    cover = framer.cover(window, (x, y, width, height))
    composited = Image.composite(cover.convert("RGB"), placed, cover.getchannel("A"))

    assert composited.tobytes() == still.tobytes()
