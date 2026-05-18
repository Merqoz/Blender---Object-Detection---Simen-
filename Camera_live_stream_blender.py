bl_info = {
    "name": "Phone Camera Stream",
    "author": "You",
    "version": (1, 3, 0),
    "blender": (3, 0, 0),
    "location": "View3D > Sidebar (N) > Phone Cam",
    "description": "Stream your phone's camera into Blender via a QR-code connection",
    "category": "Camera",
}

import bpy
import os
import sys
import ssl
import socket
import socketserver
import http.server
import threading
import subprocess
import importlib
import tempfile
import queue
import datetime
import ipaddress
import time
from io import BytesIO

from bpy.props import StringProperty, IntProperty, FloatProperty, BoolProperty


# -------------------------------------------------------------------------
# Module-level state (cleaned up in unregister())
# -------------------------------------------------------------------------
_frame_queue: "queue.Queue[bytes]" = queue.Queue(maxsize=2)
_server = None
_server_thread = None
_running = False
_last_update_ts = 0.0
_last_feed_aspect = (0, 0)
_install_thread = None  # tracks background dependency install

FEED_IMAGE_NAME = "PhoneCamFeed"
QR_IMAGE_NAME = "PhoneCamQR"


# -------------------------------------------------------------------------
# Dependencies
# -------------------------------------------------------------------------
REQUIRED = [
    ("qrcode[pil]",  "qrcode"),
    ("Pillow",       "PIL"),
    ("cryptography", "cryptography"),
]


def deps_installed() -> bool:
    for _, mod in REQUIRED:
        try:
            importlib.import_module(mod)
        except ImportError:
            return False
    return True


def install_deps():
    py = sys.executable
    try:
        subprocess.check_call([py, "-m", "ensurepip", "--upgrade"])
    except Exception:
        pass
    try:
        subprocess.check_call([py, "-m", "pip", "install", "--upgrade", "pip"])
    except Exception:
        pass
    missing = [pkg for pkg, mod in REQUIRED
               if importlib.util.find_spec(mod) is None]
    if missing:
        subprocess.check_call([py, "-m", "pip", "install", *missing])
    # Make freshly installed packages findable without restarting Blender
    importlib.invalidate_caches()


def _background_install():
    """Install missing deps without blocking the main thread."""
    try:
        if deps_installed():
            return
        print("[PhoneCam] Installing required Python packages "
              "(qrcode[pil], Pillow, cryptography)...", flush=True)
        install_deps()
        if deps_installed():
            print("[PhoneCam] Dependencies installed.", flush=True)
        else:
            print("[PhoneCam] Install ran but imports still fail. "
                  "Use the 'Install Dependencies' button to retry.", flush=True)
    except Exception as e:
        print(f"[PhoneCam] Auto-install failed: {e}", flush=True)
        print("[PhoneCam] Retry from the 'Install Dependencies' button "
              "in the N-panel.", flush=True)


# -------------------------------------------------------------------------
# Network helpers
# -------------------------------------------------------------------------
def get_local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def generate_cert(cert_path: str, key_path: str, ip: str):
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Blender Phone Cam")])
    san = x509.SubjectAlternativeName([
        x509.IPAddress(ipaddress.ip_address(ip)),
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
        x509.DNSName("localhost"),
    ])
    now = datetime.datetime.utcnow()
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .add_extension(san, critical=False)
        .sign(key, hashes.SHA256())
    )
    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(key_path, "wb") as f:
        f.write(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ))


# -------------------------------------------------------------------------
# HTML page served to the phone
# -------------------------------------------------------------------------
HTML_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Blender Phone Cam</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { font-family: -apple-system, system-ui, sans-serif; margin:0; padding:16px;
         background:#111; color:#eee; }
  h1 { font-size: 18px; margin: 0 0 12px; }
  video { width: 100%; max-width: 640px; background:#000; border-radius:8px; }
  .row { display:flex; flex-wrap:wrap; gap:8px; margin:12px 0; }
  button { flex:1; min-width:120px; padding:14px 12px; font-size:16px;
           border:0; border-radius:8px; background:#444; color:#fff; }
  button.primary { background:#3a78d8; }
  button:disabled { opacity:.5; }
  #status { font-size:14px; opacity:.85; }
  small { opacity:.6; }
</style>
</head>
<body>
<h1>Blender Phone Camera</h1>
<video id="v" autoplay playsinline muted></video>
<div class="row">
  <button id="start" class="primary">Start streaming</button>
  <button id="stop">Stop</button>
  <button id="switch">Switch cam</button>
</div>
<div id="status">Idle. Tap "Start streaming" and allow camera access.</div>
<small>This page is served by Blender on your LAN.</small>
<script>
const v = document.getElementById('v');
const statusEl = document.getElementById('status');
let stream = null, intervalId = null;
let facing = 'environment';
let sending = false;
const canvas = document.createElement('canvas');
const ctx = canvas.getContext('2d');

async function startStream() {
  try {
    stopStream();
    stream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: { ideal: facing },
               width: { ideal: 1280 }, height: { ideal: 720 } },
      audio: false
    });
    v.srcObject = stream;
    await v.play();
    canvas.width = v.videoWidth || 1280;
    canvas.height = v.videoHeight || 720;
    intervalId = setInterval(sendFrame, 1000 / 15);  // ~15 fps from phone
    statusEl.textContent = 'Streaming ' + canvas.width + ' x ' + canvas.height;
  } catch (e) {
    statusEl.textContent = 'Error: ' + e.message;
  }
}

function stopStream() {
  if (intervalId) { clearInterval(intervalId); intervalId = null; }
  if (stream) { stream.getTracks().forEach(t => t.stop()); stream = null; }
  statusEl.textContent = 'Stopped';
}

async function sendFrame() {
  if (sending || !stream || !v.videoWidth) return;
  sending = true;
  try {
    if (canvas.width !== v.videoWidth) {
      canvas.width = v.videoWidth;
      canvas.height = v.videoHeight;
    }
    ctx.drawImage(v, 0, 0, canvas.width, canvas.height);
    const blob = await new Promise(r => canvas.toBlob(r, 'image/jpeg', 0.6));
    if (blob) {
      await fetch('/frame', { method: 'POST', body: blob,
                              headers: { 'Content-Type': 'image/jpeg' } });
    }
  } catch (e) {
    statusEl.textContent = 'Send error: ' + e.message;
  } finally {
    sending = false;
  }
}

document.getElementById('start').onclick  = startStream;
document.getElementById('stop').onclick   = stopStream;
document.getElementById('switch').onclick = () => {
  facing = (facing === 'environment') ? 'user' : 'environment';
  if (stream) startStream();
};
</script>
</body>
</html>
"""


# -------------------------------------------------------------------------
# HTTP server
# -------------------------------------------------------------------------
class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args, **kwargs):
        pass  # keep Blender's console quiet

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            body = HTML_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == "/frame":
            n = int(self.headers.get("Content-Length", 0))
            data = self.rfile.read(n) if n > 0 else b""
            if data:
                try:
                    _frame_queue.put_nowait(data)
                except queue.Full:
                    try: _frame_queue.get_nowait()
                    except queue.Empty: pass
                    try: _frame_queue.put_nowait(data)
                    except queue.Full: pass
            self.send_response(204)
            self.end_headers()
        else:
            self.send_error(404)


class _ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def start_server(port: int, cert_path: str, key_path: str):
    global _server, _server_thread
    _server = _ThreadedHTTPServer(("0.0.0.0", port), _Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_path, key_path)
    _server.socket = ctx.wrap_socket(_server.socket, server_side=True)
    _server_thread = threading.Thread(target=_server.serve_forever, daemon=True)
    _server_thread.start()


def stop_server():
    global _server, _server_thread
    if _server is not None:
        try:
            _server.shutdown()
            _server.server_close()
        except Exception:
            pass
        _server = None
    _server_thread = None


# -------------------------------------------------------------------------
# Image plumbing (main thread only)
# -------------------------------------------------------------------------
def _put_pil_into_blender_image(pil_rgba, name: str):
    import numpy as np
    w, h = pil_rgba.size
    arr = np.asarray(pil_rgba, dtype=np.float32) / 255.0
    arr = np.flipud(arr)  # Blender's pixel origin is bottom-left

    img = bpy.data.images.get(name)
    if img is None:
        img = bpy.data.images.new(name, width=w, height=h, alpha=True)
    elif img.size[0] != w or img.size[1] != h:
        img.scale(w, h)

    img.pixels.foreach_set(arr.ravel())
    img.update()
    return img


def update_feed_image(jpeg_bytes: bytes):
    """Decode a JPEG frame into the PhoneCamFeed image (main thread)."""
    global _last_feed_aspect
    from PIL import Image as PILImage
    pil = PILImage.open(BytesIO(jpeg_bytes)).convert("RGBA")
    img = _put_pil_into_blender_image(pil, FEED_IMAGE_NAME)

    # Auto-match render aspect when phone rotates
    scn = bpy.context.scene
    if scn and getattr(scn, "phonecam_auto_match_aspect", False):
        new_aspect = (img.size[0], img.size[1])
        if new_aspect != _last_feed_aspect and new_aspect[0] > 0 and new_aspect[1] > 0:
            scn.render.resolution_x = new_aspect[0]
            scn.render.resolution_y = new_aspect[1]
            _last_feed_aspect = new_aspect

    # Redraw any viewports / image editors showing this
    wm = bpy.context.window_manager
    if wm:
        for window in wm.windows:
            for area in window.screen.areas:
                if area.type in {'IMAGE_EDITOR', 'VIEW_3D'}:
                    area.tag_redraw()
    return img


def make_qr_image(url: str):
    import qrcode
    qr = qrcode.QRCode(border=2, box_size=10,
                       error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(url)
    qr.make(fit=True)
    pil = qr.make_image(fill_color="black", back_color="white").convert("RGBA")
    return _put_pil_into_blender_image(pil, QR_IMAGE_NAME)


def show_image_in_editor(img):
    wm = bpy.context.window_manager
    if not wm:
        return False
    for window in wm.windows:
        for area in window.screen.areas:
            if area.type == 'IMAGE_EDITOR':
                area.spaces.active.image = img
                area.tag_redraw()
                return True
    return False


def assign_to_camera_background(img) -> bool:
    """Attach the feed image to the active camera as an on-top overlay."""
    cam = bpy.context.scene.camera
    if not cam or cam.type != 'CAMERA':
        return False
    cam_data = cam.data
    cam_data.show_background_images = True
    # Remove any prior entry for the same image (avoid stacking duplicates)
    for bg in list(cam_data.background_images):
        if bg.image and bg.image.name == img.name:
            cam_data.background_images.remove(bg)
    bg = cam_data.background_images.new()
    bg.image = img
    bg.display_depth = 'FRONT'           # draw on top of the 3D scene
    bg.frame_method = 'FIT'              # respect feed aspect inside the frame
    bg.alpha = bpy.context.scene.phonecam_opacity
    return True


def _update_opacity(self, context):
    """Live-update background alpha when the user moves the opacity slider."""
    scene = self
    cam = scene.camera
    if not cam or cam.type != 'CAMERA':
        return
    for bg in cam.data.background_images:
        if bg.image and bg.image.name == FEED_IMAGE_NAME:
            bg.alpha = scene.phonecam_opacity
    # Force viewport refresh
    wm = bpy.context.window_manager
    if wm:
        for window in wm.windows:
            for area in window.screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()


def _apply_aspect_now(scene) -> bool:
    """Set render resolution to match current feed image dimensions."""
    global _last_feed_aspect
    img = bpy.data.images.get(FEED_IMAGE_NAME)
    if not img or img.size[0] <= 0 or img.size[1] <= 0:
        return False
    scene.render.resolution_x = img.size[0]
    scene.render.resolution_y = img.size[1]
    _last_feed_aspect = (img.size[0], img.size[1])
    return True


# -------------------------------------------------------------------------
# Frame-consumer timer (main thread, throttled to capture fps)
# -------------------------------------------------------------------------
def _frame_timer():
    global _last_update_ts
    if not _running:
        return None  # unregister

    scn = bpy.context.scene
    capture_on = getattr(scn, "phonecam_capture_active", True) if scn else True
    frozen     = getattr(scn, "phonecam_frozen", False) if scn else False
    fps = max(0.1, getattr(scn, "phonecam_capture_fps", 15.0)) if scn else 15.0
    min_interval = 1.0 / fps
    now = time.monotonic()

    if not capture_on or frozen:
        # Drop everything so the queue doesn't back up while paused / frozen
        try:
            while True:
                _frame_queue.get_nowait()
        except queue.Empty:
            pass
        return 0.1

    # Take only the newest queued frame; discard older ones
    latest = None
    try:
        while True:
            latest = _frame_queue.get_nowait()
    except queue.Empty:
        pass

    if latest is not None and (now - _last_update_ts) >= min_interval:
        try:
            update_feed_image(latest)
            _last_update_ts = now
        except Exception as e:
            print("[PhoneCam] frame update error:", e)

    # Run slightly faster than the capture rate so we never add latency
    return max(0.01, min(min_interval / 2.0, 1.0 / 30.0))


# -------------------------------------------------------------------------
# Operators
# -------------------------------------------------------------------------
class PHONECAM_OT_install_deps(bpy.types.Operator):
    bl_idname = "phonecam.install_deps"
    bl_label = "Install Dependencies"
    bl_description = "Install qrcode, Pillow and cryptography into Blender's Python"

    def execute(self, context):
        try:
            install_deps()
        except Exception as e:
            self.report({'ERROR'}, f"Install failed: {e}")
            return {'CANCELLED'}
        if deps_installed():
            self.report({'INFO'}, "Dependencies installed.")
            return {'FINISHED'}
        self.report({'ERROR'}, "Install ran but imports still fail. Check console.")
        return {'CANCELLED'}


class PHONECAM_OT_start(bpy.types.Operator):
    bl_idname = "phonecam.start"
    bl_label = "Start Phone Camera"
    bl_description = "Start the HTTPS server and show the connection QR code"

    def execute(self, context):
        global _running, _last_update_ts

        if _running:
            self.report({'WARNING'}, "Already running.")
            return {'CANCELLED'}

        # Wait for background install if it's still going
        if _install_thread is not None and _install_thread.is_alive():
            self.report({'INFO'},
                "Dependencies are still installing in the background — "
                "try again in a few seconds.")
            return {'CANCELLED'}

        # Fall-back synchronous install if background install failed or never ran
        if not deps_installed():
            self.report({'INFO'}, "Installing required Python packages...")
            try:
                install_deps()
            except Exception as e:
                self.report({'ERROR'}, f"Auto-install failed: {e}")
                return {'CANCELLED'}
            if not deps_installed():
                self.report({'ERROR'},
                    "Install ran but imports still fail. Check the console.")
                return {'CANCELLED'}

        ip = get_local_ip()
        port = context.scene.phonecam_port
        url = f"https://{ip}:{port}/"

        tmp = tempfile.gettempdir()
        cert_path = os.path.join(tmp, "blender_phonecam.crt")
        key_path  = os.path.join(tmp, "blender_phonecam.key")

        try:
            generate_cert(cert_path, key_path, ip)
        except Exception as e:
            self.report({'ERROR'}, f"Certificate generation failed: {e}")
            return {'CANCELLED'}

        try:
            start_server(port, cert_path, key_path)
        except OSError as e:
            self.report({'ERROR'}, f"Could not bind port {port}: {e}")
            return {'CANCELLED'}
        except Exception as e:
            self.report({'ERROR'}, f"Server failed: {e}")
            return {'CANCELLED'}

        try:
            qr_img = make_qr_image(url)
            show_image_in_editor(qr_img)
        except Exception as e:
            self.report({'WARNING'}, f"QR generation failed: {e}")

        context.scene.phonecam_url = url
        _running = True
        _last_update_ts = 0.0
        if not bpy.app.timers.is_registered(_frame_timer):
            bpy.app.timers.register(_frame_timer, first_interval=0.1)

        self.report({'INFO'}, f"Listening on {url}")
        return {'FINISHED'}


class PHONECAM_OT_stop(bpy.types.Operator):
    bl_idname = "phonecam.stop"
    bl_label = "Stop Phone Camera"
    bl_description = "Stop the server and frame timer"

    def execute(self, context):
        global _running
        _running = False
        stop_server()
        if bpy.app.timers.is_registered(_frame_timer):
            try:
                bpy.app.timers.unregister(_frame_timer)
            except Exception:
                pass
        self.report({'INFO'}, "Stopped.")
        return {'FINISHED'}


class PHONECAM_OT_assign_camera_bg(bpy.types.Operator):
    bl_idname = "phonecam.assign_camera_bg"
    bl_label = "Set As Camera Overlay"
    bl_description = "Use the phone feed as the active camera's overlay (with the opacity slider)"

    def execute(self, context):
        img = bpy.data.images.get(FEED_IMAGE_NAME)
        if img is None:
            self.report({'ERROR'},
                "No phone feed yet. Start the server and connect your phone first.")
            return {'CANCELLED'}
        if assign_to_camera_background(img):
            self.report({'INFO'}, "Overlay attached to active camera.")
            return {'FINISHED'}
        self.report({'ERROR'}, "No active camera in the scene.")
        return {'CANCELLED'}


class PHONECAM_OT_show_qr(bpy.types.Operator):
    bl_idname = "phonecam.show_qr"
    bl_label = "Show QR In Image Editor"
    bl_description = "Open the QR code image in any open Image Editor"

    def execute(self, context):
        img = bpy.data.images.get(QR_IMAGE_NAME)
        if img is None:
            self.report({'ERROR'}, "No QR yet — start the server first.")
            return {'CANCELLED'}
        if show_image_in_editor(img):
            return {'FINISHED'}
        self.report({'WARNING'},
            "No Image Editor open. Split an area, set it to 'Image Editor', "
            "and pick 'PhoneCamQR' from the image dropdown.")
        return {'CANCELLED'}


class PHONECAM_OT_match_aspect(bpy.types.Operator):
    bl_idname = "phonecam.match_aspect"
    bl_label = "Match Camera To Feed"
    bl_description = "Resize the render frame to match the current phone-feed aspect (portrait/landscape)"

    def execute(self, context):
        if _apply_aspect_now(context.scene):
            r = context.scene.render
            self.report({'INFO'}, f"Render set to {r.resolution_x} x {r.resolution_y}")
            return {'FINISHED'}
        self.report({'ERROR'},
            "No feed image yet — start the server and stream a frame first.")
        return {'CANCELLED'}


# -------------------------------------------------------------------------
# UI
# -------------------------------------------------------------------------
class PHONECAM_PT_panel(bpy.types.Panel):
    bl_label = "Phone Camera Stream"
    bl_idname = "PHONECAM_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Phone Cam"

    def draw(self, context):
        layout = self.layout
        scn = context.scene

        if not deps_installed():
            box = layout.box()
            box.label(text="First-time setup", icon='INFO')
            box.label(text="Install required Python packages:")
            box.operator("phonecam.install_deps", icon='SCRIPT')
            return

        # --- Server ---
        layout.prop(scn, "phonecam_port")
        row = layout.row(align=True)
        if not _running:
            row.operator("phonecam.start", icon='PLAY', text="Start")
        else:
            row.operator("phonecam.stop", icon='PAUSE', text="Stop")

        if _running and scn.phonecam_url:
            box = layout.box()
            box.label(text="Scan QR or visit:", icon='URL')
            box.label(text=scn.phonecam_url)
            box.operator("phonecam.show_qr", icon='IMAGE_DATA')
            box.label(text="(Accept the cert warning on your phone)",
                      icon='ERROR')

        layout.separator()

        # --- Capture ---
        box = layout.box()
        box.label(text="Capture", icon='RENDER_STILL')
        cap_text = "Capturing — tap to pause" if scn.phonecam_capture_active else "Capture paused — tap to start"
        cap_icon = 'RADIOBUT_ON' if scn.phonecam_capture_active else 'RADIOBUT_OFF'
        box.prop(scn, "phonecam_capture_active",
                 toggle=True, text=cap_text, icon=cap_icon)
        row = box.row()
        row.enabled = scn.phonecam_capture_active
        row.prop(scn, "phonecam_capture_fps", slider=True)
        if scn.phonecam_capture_active:
            interval = 1.0 / max(0.1, scn.phonecam_capture_fps)
            box.label(text=f"≈ one frame every {interval:.2f} s",
                      icon='TIME')

        # --- Camera overlay ---
        box = layout.box()
        box.label(text="Camera Overlay", icon='OUTLINER_OB_CAMERA')
        box.operator("phonecam.assign_camera_bg", icon='IMAGE_PLANE')

        if scn.phonecam_frozen:
            freeze_text = "Frozen — click to resume live"
            freeze_icon = 'PLAY'
        else:
            freeze_text = "Freeze Frame"
            freeze_icon = 'PAUSE'
        box.prop(scn, "phonecam_frozen",
                 toggle=True, text=freeze_text, icon=freeze_icon)

        box.prop(scn, "phonecam_opacity", slider=True)
        box.label(text="(Visible in camera view — numpad 0)", icon='INFO')

        # --- Aspect ---
        box = layout.box()
        box.label(text="Frame Aspect", icon='FULLSCREEN_ENTER')
        box.operator("phonecam.match_aspect", icon='ARROW_LEFTRIGHT')
        box.prop(scn, "phonecam_auto_match_aspect")

        if bpy.data.images.get(FEED_IMAGE_NAME):
            img = bpy.data.images[FEED_IMAGE_NAME]
            layout.label(text=f"Feed: {img.size[0]} x {img.size[1]}",
                         icon='IMAGE_RGB')


# -------------------------------------------------------------------------
# Register
# -------------------------------------------------------------------------
classes = (
    PHONECAM_OT_install_deps,
    PHONECAM_OT_start,
    PHONECAM_OT_stop,
    PHONECAM_OT_assign_camera_bg,
    PHONECAM_OT_show_qr,
    PHONECAM_OT_match_aspect,
    PHONECAM_PT_panel,
)


def register():
    global _install_thread
    for c in classes:
        bpy.utils.register_class(c)

    bpy.types.Scene.phonecam_port = IntProperty(
        name="Port", default=8443, min=1024, max=65535,
        description="TCP port the local HTTPS server will listen on")

    bpy.types.Scene.phonecam_url = StringProperty(
        name="URL", default="")

    bpy.types.Scene.phonecam_opacity = FloatProperty(
        name="Opacity",
        default=0.5, min=0.0, max=1.0, subtype='FACTOR',
        description="Transparency of the phone feed over the 3D scene",
        update=_update_opacity)

    bpy.types.Scene.phonecam_capture_active = BoolProperty(
        name="Capture",
        description="When on, frames update the feed image at the chosen rate",
        default=True)

    bpy.types.Scene.phonecam_capture_fps = FloatProperty(
        name="Frames per second",
        default=15.0, min=0.1, max=30.0,
        precision=2,
        description="How often to apply a new phone frame "
                    "(e.g. 1.0 = one snapshot per second)")

    bpy.types.Scene.phonecam_auto_match_aspect = BoolProperty(
        name="Auto-match when phone rotates",
        description="Resize the render frame automatically whenever the phone is rotated",
        default=False)

    bpy.types.Scene.phonecam_frozen = BoolProperty(
        name="Frozen",
        description="Freeze the current frame on the camera; toggle off to resume the live feed",
        default=False)

    # Auto-install missing Python dependencies in the background so addon
    # enable isn't blocked. The Start operator also retries synchronously
    # if needed.
    if not deps_installed():
        _install_thread = threading.Thread(target=_background_install, daemon=True)
        _install_thread.start()


def unregister():
    global _running
    _running = False
    stop_server()
    if bpy.app.timers.is_registered(_frame_timer):
        try:
            bpy.app.timers.unregister(_frame_timer)
        except Exception:
            pass
    for c in reversed(classes):
        try:
            bpy.utils.unregister_class(c)
        except Exception:
            pass
    for prop in ("phonecam_port", "phonecam_url", "phonecam_opacity",
                 "phonecam_capture_active", "phonecam_capture_fps",
                 "phonecam_auto_match_aspect", "phonecam_frozen"):
        if hasattr(bpy.types.Scene, prop):
            delattr(bpy.types.Scene, prop)


if __name__ == "__main__":
    register()