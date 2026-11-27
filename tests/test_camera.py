"""Camera tab: layout, the serial demux, capture, viewer and saving.

Run:  QT_QPA_PLATFORM=offscreen xvfb-run -a python tests/test_camera.py
"""
import os
import shutil
import sys
import tempfile
import tkinter as tk

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import kestrelsat.app as sg
from kestrelsat import arducam, camera as camera_mod, scaling

failures = []


class FakeMessagebox:
    """Records dialogs instead of opening them.

    A real messagebox runs a nested event loop and blocks until someone
    clicks, which in a headless test is an indefinite hang. Every modal the
    tab can raise is captured here and answered from .answer.
    """

    def __init__(self):
        self.calls = []
        self.answer = True

    def __getattr__(self, name):
        if name.startswith(("show", "ask")):
            def call(title, message, **kwargs):
                self.calls.append((name, title, message))
                return self.answer
            return call
        raise AttributeError(name)

    def titles(self):
        return [title for _, title, _ in self.calls]

    def clear(self):
        self.calls.clear()


fake_mb = FakeMessagebox()
camera_mod.messagebox = fake_mb


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def tabs(app):
    return [app.notebook.tab(t, "text") for t in app.notebook.tabs()]


def pump(times=6):
    """Let the rx pump and any after() callbacks run."""
    for _ in range(times):
        root.update()
        root.after(30, root.quit)
        root.mainloop()


if os.path.exists(sg.SETTINGS_FILE):
    os.remove(sg.SETTINGS_FILE)

root = tk.Tk()
root.geometry("1100x700")
app = sg.SerialGUI(root)
root.update()
cam = app.camera


print("\n-- tab and layout --")
check(cam is not None, "the app owns a CameraTab")
check(tabs(app) == ["Connection", "Notepad", "Camera", "Plot 1", "  +  "],
      "Camera sits left of the plots and '+': %s" % tabs(app))
slaves = cam.frame.pack_slaves()
check(slaves and slaves[-1] is cam.viewer,
      "the viewer is packed last, so it absorbs the leftover cavity")
check(str(cam.viewer.pack_info()["expand"]) in ("1", "True"),
      "and it is the only expanding child")
for name, widget in (("banner", cam.banner), ("strip", cam.strip)):
    check(widget.winfo_manager() == "pack", f"{name} is packed")

# Every fixed row must survive a short window - the rule test_resize enforces.
# The page must be selected first: an unselected notebook tab is 1px.
app.notebook.select(cam.frame)
root.geometry("820x340")
root.update()
for name, widget in (("banner", cam.banner), ("strip", cam.strip),
                     ("viewer", cam.viewer)):
    check(widget.winfo_ismapped() and widget.winfo_height() > 0,
          f"{name} still visible at 820x340 ({widget.winfo_height()}px)")
root.geometry("1100x700")
root.update()


print("\n-- controls follow connection state --")
check(str(cam.take_btn["state"]) == "disabled",
      "Take Picture is disabled while disconnected")
check(cam.banner_var.get().startswith("Not connected"),
      "the banner says so plainly")
check(not cam.busy, "an idle tab is not busy")

# Baud only matters on a USB-serial bridge (ESP32 Thing Plus). The RP2040
# Adalogger's native USB ignores it, so the banner must not nag there.
from types import SimpleNamespace
real_list_ports = camera_mod.list_ports
real_port, real_baud = app.port_var.get(), app.baud_var.get()
def fake_ports(vid):
    return SimpleNamespace(comports=lambda: [SimpleNamespace(device="COM9", vid=vid)])
app.port_var.set("COM9")
app.baud_var.set("9600")
app.is_connected, app.test_mode = True, False
camera_mod.list_ports = fake_ports(0x239A)      # Adafruit Feather RP2040
cam.update_banner()
check(cam.banner_var.get() == "", "no baud warning on the RP2040's native USB")
camera_mod.list_ports = fake_ports(0x1A86)      # CH340 USB-serial bridge
cam.update_banner()
check("115200" in cam.banner_var.get(),
      "a bridged board at 9600 is told the camera wants 115200")
app.baud_var.set("115200")
cam.update_banner()
check(cam.banner_var.get() == "", "and 115200 on a bridge is fine")
camera_mod.list_ports = real_list_ports
app.port_var.set(real_port)
app.baud_var.set(real_baud)
app.is_connected = False
cam.update_banner()


print("\n-- the demux contract --")
JPEG = None
if camera_mod.QT_IMAGING_AVAILABLE:
    JPEG = cam.make_test_pattern(0x03, arducam.PIX_FMT_JPEG)
check(JPEG is not None and JPEG[:2] == b"\xff\xd8",
      "a synthetic test pattern encodes to JPEG")

TELEM = b"TIME:1.0,SENSOR_A:12.5\n"
before_lines = app.plots[0].channels.get("SENSOR_A")
stream = (TELEM + arducam.pack_image_packet(0x03, arducam.PIX_FMT_JPEG, JPEG)
          + TELEM)
app.is_connected = True
app.test_mode = True
cam.on_connect()
residual = cam.consume(stream)
check(residual == TELEM * 2,
      "telemetry either side of an image comes back byte-identical")
check(cam.current is not None and cam.current.data == JPEG,
      "and the image is extracted intact")
check(b"\xff\xd8" not in residual, "no image bytes leak to the monitor")

# The regression that motivated the whole design: a JPEG containing ZMODEM's
# receive offer must not be able to start a transfer.
poisoned = b"\xff\xd8" + b"**\x18B00" + b"\xff\xd9"
residual = cam.consume(arducam.pack_image_packet(0x03, arducam.PIX_FMT_JPEG, poisoned))
check(b"**\x18B00" not in residual,
      "a ZMODEM offer inside a JPEG never reaches display_received_data")
check(not app._transfer_active.is_set(), "so no transfer is started")

# And the whole path, through the real queue and pump.
app._post_rx(("data", TELEM + arducam.pack_image_packet(
    0x05, arducam.PIX_FMT_JPEG, JPEG) + TELEM))
pump()
check(cam.current is not None and cam.current.data == JPEG,
      "an image posted through _post_rx reaches the tab via _pump_rx")
check("SENSOR_A" in app.plots[0].channels,
      "and the interleaved telemetry still reaches the plots")


print("\n-- decode, score and display --")
capture = cam.current
check(capture.image is not None, "the capture decoded to a QImage")
check((capture.width, capture.height) == (320, 240),
      "dimensions come from the decoded image, not the commanded mode "
      f"({capture.width}x{capture.height} for a frame sent as mode 0x05)")
check(capture.sharpness is not None and capture.sharpness > 0,
      f"a sharpness score was computed ({capture.sharpness:,.0f})")
root.update()
check(cam._photo is not None, "something was rendered into the viewer")

# A blurred frame must score lower, or the focus meter is useless.
if camera_mod.QT_IMAGING_AVAILABLE and camera_mod.NUMPY_AVAILABLE:
    from PyQt5.QtCore import Qt
    sharp_score = capture.sharpness
    blurred = camera_mod.Capture(
        data=b"", pix_fmt=arducam.PIX_FMT_JPEG, mode=0x03, width=320, height=240)
    blurred.image = capture.image.scaled(
        40, 30, Qt.IgnoreAspectRatio, Qt.SmoothTransformation).scaled(
        320, 240, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    cam._score(blurred)
    check(blurred.sharpness < sharp_score,
          f"a blurred frame scores lower ({blurred.sharpness:,.0f} < "
          f"{sharp_score:,.0f}) - focus assist actually discriminates")


print("\n-- viewer: zoom, pan and the ruler --")
cam.zoom_fit()
check(cam._fit, "Fit sets fit mode")
cam.zoom_actual()
check(not cam._fit and abs(cam._scale() - 1.0) < 1e-9, "1:1 means scale 1.0")
cam.zoom_step(1)
check(cam._scale() > 1.0, f"zooming in raises the scale ({cam._scale()})")
cam.zoom_step(-1)
check(abs(cam._scale() - 1.0) < 1e-9, "and stepping back returns to 1:1")
for _ in range(10):
    cam.zoom_step(1)
check(cam._scale() <= max(camera_mod.ZOOM_STEPS) + 1e-9, "zoom is bounded above")
for _ in range(20):
    cam.zoom_step(-1)
check(cam._scale() >= min(camera_mod.ZOOM_STEPS) - 1e-9, "and bounded below")

cam.zoom_actual()
root.update()
# Round-tripping a view coordinate through image space must be identity.
cam._render()
ix, iy = cam._view_to_image(100, 80)
vx, vy = cam._image_to_view((ix, iy), cam._scale())
check(abs(vx - 100) < 0.01 and abs(vy - 80) < 0.01,
      "view <-> image coordinates round-trip")

cam._ruler = [(10.0, 20.0), (13.0, 24.0)]
(ax, ay), (bx, by) = cam._ruler
dist = ((bx - ax) ** 2 + (by - ay) ** 2) ** 0.5
check(abs(dist - 5.0) < 1e-9, "a 3-4-5 ruler span measures 5 px")
cam.clear_ruler()
check(cam._ruler == [] and cam.readout_var.get() == "", "clearing the ruler works")

# Panning must stay inside the image.
cam._pan_from = (0, 0, (0.5, 0.5))

class _E:
    x = -100000
    y = -100000

cam._on_drag(_E())
check(0.0 <= cam._centre[0] <= 1.0 and 0.0 <= cam._centre[1] <= 1.0,
      f"panning is clamped to the image ({cam._centre})")
cam._pan_from = None
cam.zoom_fit()


print("\n-- command framing and pacing --")
sent = []
app.test_mode = False
real_write = app.write_bytes
app.write_bytes = lambda data: (sent.append(bytes(data)), True)[1]
cam._cmd_queue.clear()
cam.take_picture()
pump(30)          # longer than RESOLUTION_SETTLE_MS
check(len(sent) >= 3, f"take_picture queues several frames (got {len(sent)})")
check(all(f[0] == 0x55 and f[-1] == 0xAA for f in sent),
      "every frame is bracketed by 55/AA")
check(arducam.set_picture_resolution(arducam.PIX_FMT_JPEG, arducam.MODE_QXGA) in sent,
      "including the JPEG 2048x1536 resolution frame")
check(arducam.take_picture() in sent, "and the take-picture frame")
check(sent.index(arducam.set_picture_resolution(
          arducam.PIX_FMT_JPEG, arducam.MODE_QXGA)) < sent.index(arducam.take_picture()),
      "resolution is set before the shot is taken")

# Preview at full resolution starts straight away: there is no "this will be
# slow" question any more (the RP2040 previews 2048x1536 at about 1 fps).
sent.clear()
cam._reset_transfer()
cam._cancelling = False
fake_mb.clear()
cam.mode_var.set("2048x1536")
cam.toggle_preview()
pump(4)
check(not fake_mb.calls and cam._streaming,
      "a full-resolution preview starts without asking")
check(arducam.set_video_resolution(arducam.MODE_QXGA) in sent,
      "and sends the full-resolution video command")
cam._end_preview()
cam._reset_transfer()
cam.preview_btn.configure(text="Start Preview")

sent.clear()
cam.mode_var.set("320x240")
cam.toggle_preview()
pump(6)
check(cam._streaming, "preview at 320x240 starts without asking")
check(arducam.set_video_resolution(0x03) in sent,
      "and sends the video-resolution frame")
sent.clear()
cam.toggle_preview()
pump(16)          # longer than STREAM_STOP_MS
check(arducam.stop_stream() in sent, "and stopping sends the stop-stream frame")
cam._streaming = False
cam.mode_var.set("2048x1536")
app.write_bytes = real_write


print("\n-- preview keeps up with a fast link --")
# The RP2040 Adalogger streams preview frames faster than any GUI can decode
# and draw them. Simulate a burst of 60 frames arriving at once, as wire-sized
# chunks through the real receive queue, with the pump running.
import time as _time
cam.mode_var.set("320x240")
cam.pix_fmt_var.set("RGB565")     # preview is JPEG whatever the still format
strip_before = list(cam.captures)
cam._streaming = True
cam._preview_frame = cam._preview_latest = None
cam._preview_scores = []
cam._preview_rx = cam._preview_shown = 0
frames = [cam.make_test_pattern(0x03, arducam.PIX_FMT_JPEG) for _ in range(2)]
# Tag each frame so the newest one is recognisable: bytes after FF D9 are
# ignored by a JPEG decoder.
burst = b"".join(arducam.pack_preview_packet(arducam.PIX_FMT_JPEG,
                                             frames[0] + i.to_bytes(4, "big"))
                 for i in range(60))
for offset in range(0, len(burst), 16384):
    app._post_rx(("data", burst[offset:offset + 16384]))
pump(3)
check(app._rx_queue.empty(), "a 60-frame burst is drained from the queue at once")
check(cam._preview_rx == 60, f"every frame is parsed ({cam._preview_rx}/60)")
pump(12)
check(0 < cam._preview_shown < 60,
      f"but only the newest are drawn ({cam._preview_shown} of 60)")
check(cam.current is not None and cam.current.data[-4:] == (59).to_bytes(4, "big"),
      "the frame on screen is the newest one")
check(cam.current.image is not None and cam.current.pix_fmt == arducam.PIX_FMT_JPEG,
      "decoded as JPEG even though the still format is RGB565")
check(cam.captures == strip_before,
      "preview frames do not flood the capture strip")
check(cam._preview_scores, "each drawn frame feeds the live sharpness bars")

# Stopping. The firmware sends preview a few hundred bytes at a time between
# command checks, so Stop usually lands mid-frame: half a frame, then the bare
# trailer and the streamoff packet. That used to leave the tab "busy" forever.
cam.consume(arducam.pack_preview_packet(arducam.PIX_FMT_JPEG, frames[0])[:3000])
cam.consume(arducam.pack_stream_off())
pump(2)
check(not cam.busy, "a stop that lands mid-frame does not leave the tab busy")
check(str(cam.take_btn["state"]) == "normal" and str(cam.preview_btn["state"]) == "normal",
      "and Take Picture and Start Preview are usable again")
check(not cam._streaming and cam._preview_timer is None,
      "streamoff ends preview and cancels the pending draw")
check(len(cam.captures) == min(len(strip_before) + 1, camera_mod.MAX_CAPTURES)
      and cam.captures[-1].data[-4:] == (59).to_bytes(4, "big"),
      "and the last frame joins the strip, so it can be saved")
check(cam.current is cam.captures[-1], "and stays on screen")
cam.pix_fmt_var.set("JPEG")
cam.mode_var.set("2048x1536")

# If the MCU never confirms the stop, the tab gives up after a few seconds
# rather than staying busy.
real_write = app.write_bytes
app.write_bytes = lambda payload: None
cam._streaming = True
cam.parser.watch_stream_off = True
cam.consume(arducam.pack_preview_packet(arducam.PIX_FMT_JPEG, frames[0])[:3000])
cam.toggle_preview()                       # sends stop; no reply will come
pump(2)
check(cam._streaming, "Stop Preview waits for the camera's reply at first")
cam._stop_requested_at -= camera_mod.STREAM_STOP_TIMEOUT + 1
cam._check_watchdog()
check(not cam.busy and not cam.parser.in_image,
      "but with no reply it stops anyway and drops the half-read frame")
app.write_bytes = real_write


print("\n-- watchdog --")
cam._reset_transfer()
cam._busy = True
cam._cancelling = False
cam._await_since = 0.1          # far in the past
cam._rx_total = 0
cam._check_watchdog()
check(not cam._busy, "a capture that never answers is failed, not hung")
check("No response" in cam.status_var.get(),
      "and the message names the likely causes")
check(str(cam.cancel_btn["state"]) == "disabled", "Cancel is disabled again")

cam._busy = True
cam._rx_total = 1000
cam._rx_got = 400
cam._last_rx = 0.1
cam._check_watchdog()
check(not cam._busy and "stalled" in cam.status_var.get(),
      "a stalled transfer is failed with its byte count")


print("\n-- ZMODEM cross-guard --")
cam._busy = True
check(cam.busy, "a capture in flight marks the tab busy")
app.zmodem_enabled = True
app.is_connected = True
app.test_mode = False
app_mb = FakeMessagebox()
real_app_mb = sg.messagebox
sg.messagebox = app_mb
try:
    ready = app._transfer_ready("sending")
finally:
    sg.messagebox = real_app_mb
check(ready is False and "Camera busy" in app_mb.titles(),
      "a ZMODEM transfer is refused while a capture is in flight")
cam._busy = False


print("\n-- saving --")
tmp = tempfile.mkdtemp()
real_captures = app.captures_dir
app.captures_dir = tmp
try:
    cam.current = capture
    capture.distance = "0.5m"
    name = cam.default_filename(capture)
    check(name.endswith(".jpg"), f"a JPEG capture defaults to .jpg ({name})")
    check("0.5m" in name, "and the distance is in the filename, as the lab asks")
    check("320x240" in name, "along with the resolution")

    capture.data = JPEG
    cam.autosave_var.set(True)
    cam.save_image(auto=True)
    written = os.listdir(tmp)
    check(len(written) == 1, f"auto-save wrote one file ({written})")
    path = os.path.join(tmp, written[0])
    check(open(path, "rb").read() == JPEG,
          "and the JPEG is byte-identical to what the camera sent - no re-encode")

    # A second save must not clobber the first.
    cam.save_image(auto=True)
    check(len(os.listdir(tmp)) == 2, "a second auto-save gets a unique name")
finally:
    app.captures_dir = real_captures
    cam.autosave_var.set(False)
    shutil.rmtree(tmp, ignore_errors=True)


print("\n-- resolution list follows the module --")
cam.info = arducam.parse_camera_info(
    "Camera Type:3MP\r\nCamera Support Resolution:8191\r\n")
cam._refresh_modes()
values = list(cam.mode_combo["values"])
check("2048x1536" in values, "a 3MP module offers 2048x1536")
check("2592x1944" not in values, "but not the 5MP-only 2592x1944")
cam.info = arducam.parse_camera_info("Camera Support Resolution:8\r\n")
cam._refresh_modes()
check(list(cam.mode_combo["values"]) == ["320x240"],
      "a module reporting one resolution offers exactly that one")
check(cam.mode_var.get() == "320x240",
      "and a now-unsupported selection is moved to a valid one")


print("\n-- theme cycling --")
ok = True
for name in ("light", "high_contrast", "dark"):
    try:
        app.theme_var.set(name)
        app.on_theme_selected()
        root.update()
    except Exception as exc:
        ok = False
        print(f"       {name} raised {exc}")
check(ok, "cycling themes does not break the camera canvases")
check(cam.viewer.cget("background") != "", "the viewer keeps a backdrop after the walk")


print("\n-- dialog hygiene --")
before = set(map(str, root.winfo_children()))
cam.show_info()
root.update()
dialogs = [w for w in root.winfo_children()
           if isinstance(w, tk.Toplevel) and str(w) not in before]
check(len(dialogs) == 1, "Camera Info opens exactly one dialog")
if dialogs:
    dlg = dialogs[0]
    check(bool(dlg.bind("<Escape>")), "it is bound to Escape, like every other dialog")
    dlg.destroy()


print("\n-- deleting from the history --")
app.notebook.select(cam.frame)
root.update()


def make_capture(tag):
    """A small decoded capture, told apart by its data."""
    c = camera_mod.Capture(data=JPEG + tag, pix_fmt=arducam.PIX_FMT_JPEG,
                           mode=0x03, width=320, height=240)
    return c


def click(x, y):
    """Feed a click at strip coordinates to the strip handler."""
    event = type("E", (), {"x": int(x), "y": int(y)})()
    cam._on_strip_click(event)


cam.captures = []
cam.current = None
for tag in (b"A", b"B", b"C", b"D"):
    cam.add_capture(make_capture(tag))
a, b, c, d = cam.captures
check(cam.current is d and str(cam.clear_all_btn["state"]) == "normal",
      "four pictures in the history, the newest shown, Clear all enabled")
check(cam.strip.find_overlapping(*cam._delete_box(
    scaling.px(app.ui_scale, 6))),
      "each thumbnail has an x badge drawn in its corner")

# Clicking a thumbnail body still just shows it.
size = scaling.px(app.ui_scale, camera_mod.THUMB_PX) + scaling.px(app.ui_scale, 8)
left = scaling.px(app.ui_scale, 6)
click(left + 4, scaling.px(app.ui_scale, 6) + size // 2)
check(cam.current is a and len(cam.captures) == 4,
      "clicking a thumbnail (not its x) shows it and deletes nothing")

# Deleting one that is not on screen leaves the viewer alone.
fake_mb.clear()
bx0, by0, bx1, by1 = cam._delete_box(left + 2 * size)
click((bx0 + bx1) / 2, (by0 + by1) / 2)
check(cam.captures == [a, b, d] and cam.current is a,
      "the x on another picture removes just that one; the view is unchanged")
check(fake_mb.calls == [], "and asks nothing")

# Deleting the one on screen shows its neighbour.
cam.delete_capture(a)
check(cam.captures == [b, d] and cam.current is b,
      "deleting the picture on screen shows the next one along")
cam.delete_capture(d)
check(cam.current is b, "deleting the newest while it is not shown changes nothing else")
cam._show_capture(b)

# Saved files stay on disk.
tmp = tempfile.mkdtemp()
try:
    saved = os.path.join(tmp, "kept.jpg")
    with open(saved, "wb") as handle:
        handle.write(JPEG)
    b.saved_path = saved
    cam.delete_capture(b)
    check(os.path.exists(saved), "a deleted picture's saved file is left on disk")
    check("still on disk" in cam.status_var.get(), "and the status line says so")
finally:
    shutil.rmtree(tmp, ignore_errors=True)
check(cam.captures == [] and cam.current is None,
      "deleting the last picture empties the viewer")
check(str(cam.save_btn["state"]) == "disabled"
      and str(cam.clear_all_btn["state"]) == "disabled",
      "with Save and Clear all disabled, as there is nothing to act on")

# Clear all asks first, and No keeps everything.
for tag in (b"E", b"F", b"G"):
    cam.add_capture(make_capture(tag))
kept = list(cam.captures)
fake_mb.clear()
fake_mb.answer = False
cam.clear_history()
check([n for n, _, _ in fake_mb.calls] == ["askyesno"],
      "Clear all asks for confirmation")
check("not deleted" in fake_mb.calls[0][2],
      "and the question says saved files are not deleted")
check(cam.captures == kept, "answering No keeps every picture")
fake_mb.answer = True
cam.clear_history()
check(cam.captures == [] and cam.current is None,
      "answering Yes empties the history and the viewer")
fake_mb.clear()
cam.clear_history()
check(fake_mb.calls == [], "Clear all on an empty history does not ask")

# During preview the viewer shows the live frame, which is not in the history.
cam.add_capture(make_capture(b"H"))
live = make_capture(b"live")
cam._streaming = True
cam.current = live
cam.clear_history()
check(cam.captures == [] and cam.current is live,
      "clearing during preview leaves the live frame on screen")
cam._streaming = False
cam.current = None
root.update()


print("\n-- teardown --")
cam.destroy()
check(cam._tick_timer is None and cam._cmd_timer is None,
      "destroy() cancels the tab's timers")
check(cam.captures == [] and cam.current is None,
      "and releases the captured images")
app.camera = None

print()
if failures:
    print(f"FAILED ({len(failures)}):")
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("test_camera: all checks passed.")
