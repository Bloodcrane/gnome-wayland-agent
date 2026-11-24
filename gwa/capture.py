"""Frames out of a portal PipeWire stream, via GStreamer.

The portal hands back a PipeWire node id; `pipewiresrc path=<id>` connects to
the same session PipeWire daemon and receives the compositor's frames.  Using
gst-launch-1.0 as a subprocess keeps the package dependency-free (no
gst-python bindings needed) and makes each grab independent: a hung grab can
be killed with a timeout without tearing down the portal session.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time


class CaptureError(RuntimeError):
    pass


def have_gst() -> bool:
    return shutil.which("gst-launch-1.0") is not None


def _base(node_id: int, fd: int | None) -> list[str]:
    src = ["pipewiresrc", f"path={node_id}"]
    if fd is not None:
        src = ["pipewiresrc", f"fd={fd}", f"path={node_id}"]
    return ["gst-launch-1.0", "-q", *src]


def png_size(path: str) -> tuple[int, int]:
    """Read width/height straight from the PNG IHDR (no image library)."""
    with open(path, "rb") as fh:
        head = fh.read(24)
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
        raise CaptureError(f"{path} is not a PNG file")
    return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")


def grab(
    node_id: int,
    out_path: str,
    max_width: int | None = None,
    timeout: float = 25.0,
    fd: int | None = None,
    size: tuple[int, int] | None = None,
) -> dict:
    """Write one PNG frame. Returns {'path', 'bytes', 'width', 'height'}."""
    if not have_gst():
        raise CaptureError(
            "gst-launch-1.0 not found — install gstreamer + gst-plugin-pipewire"
        )
    out_path = os.path.abspath(os.path.expanduser(out_path))
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    if os.path.exists(out_path):
        os.unlink(out_path)

    cmd = _base(node_id, fd) + ["num-buffers=1", "!", "videoconvert"]
    if max_width and (not size or not size[0]):
        raise CaptureError("downscaling needs the stream size from the portal")
    if size and size[0] and size[1]:
        # Always pin the output to the stream size (or max_width below it).
        # With fractional scaling the compositor sends frames at the panel's
        # physical resolution (e.g. 3840x2160 at 125%) while the stream size,
        # and so the click space, is logical (3072x1728). Without this the PNG
        # would not match the coordinate contract. videoscale is a passthrough
        # when the frame already has this size.
        width = min(int(max_width), size[0]) if max_width else size[0]
        # Compute both dimensions up front: letting videoscale fixate a width
        # alone overflows on some caps and yields a 1-pixel-tall frame.
        height = max(1, round(size[1] * width / size[0]))
        cmd += [
            "!", "videoscale", "add-borders=false", "!",
            f"video/x-raw,width={int(width)},height={int(height)},"
            "pixel-aspect-ratio=1/1",
        ]
    cmd += ["!", "pngenc", "snapshot=true", "!", "filesink", f"location={out_path}"]

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        close_fds=fd is None,
    )
    try:
        _, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise CaptureError(
            f"timed out after {timeout:.0f}s waiting for a frame from stream {node_id}"
        ) from None

    if proc.returncode != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        detail = (err or b"").decode("utf-8", "replace").strip().splitlines()
        detail = " | ".join(detail[-4:]) if detail else f"exit {proc.returncode}"
        raise CaptureError(f"gstreamer could not grab a frame: {detail}")

    width, height = png_size(out_path)
    if width < 16 or height < 16:
        raise CaptureError(
            f"grab produced a degenerate {width}x{height} frame — the stream "
            "negotiated wrong (try again without --max-width)"
        )
    return {
        "path": out_path,
        "bytes": os.path.getsize(out_path),
        "width": width,
        "height": height,
    }


def record(
    node_id: int,
    out_path: str,
    duration: float | None = None,
    fps: int = 30,
    fd: int | None = None,
    extra_args: list[str] | None = None,
) -> dict:
    """Record the stream to WebM (VP8). duration=None records until Ctrl-C."""
    if not have_gst():
        raise CaptureError("gst-launch-1.0 not found")
    out_path = os.path.abspath(os.path.expanduser(out_path))
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

    # MP4/H.264 is not universally available in gst-plugins-good/ugly builds;
    # VP8 + WebM is present wherever gst-plugins-good is.
    if out_path.endswith(".mp4"):
        mux = ["x264enc", "tune=zerolatency", "speed-preset=ultrafast", "bitrate=4000",
               "!", "mp4mux"]
    else:
        mux = ["vp8enc", "deadline=1", "cpu-used=8", "!", "webmmux"]

    cmd = _base(node_id, fd) + [
        "!", "videoconvert", "!", "videorate", "!", "video/x-raw,framerate=%d/1" % fps,
        "!", *mux, "!", "filesink", f"location={out_path}",
    ]
    if extra_args:
        cmd += extra_args
    started = time.time()
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            close_fds=fd is None)
    try:
        if duration:
            time.sleep(duration)
            proc.send_signal(signal.SIGINT)
        _, err = proc.communicate()
    except KeyboardInterrupt:
        proc.send_signal(signal.SIGINT)
        _, err = proc.communicate()
    size = os.path.getsize(out_path) if os.path.exists(out_path) else 0
    if size == 0:
        detail = (err or b"").decode("utf-8", "replace").strip().splitlines()
        raise CaptureError(
            "recording produced no data: " + (" | ".join(detail[-6:]) or "unknown error")
        )
    return {
        "path": out_path,
        "bytes": size,
        "seconds": round(time.time() - started, 1),
    }
