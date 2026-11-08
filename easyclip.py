#!/usr/bin/env python3
"""EasyClip: a simple clipboard history for Linux desktops (GTK3, X11).

Press Super+V: recent copies (text and images), type to search, click or Enter to paste into the
app you were in. Pin what you reuse. Skips secrets copied from password managers.
Runs quietly in the background (`easyclip --hidden` at login); `easyclip --show` toggles the panel.
"""
import hashlib, json, os, shutil, subprocess, sys, threading, time, uuid
import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gtk, Gdk, Gio, GLib, GdkPixbuf, Pango
try:
    gi.require_version("XApp", "1.0")
    from gi.repository import XApp  # Cinnamon/MATE/Xfce tray icon
except (ValueError, ImportError):
    XApp = None

VERSION = "1.2.0"
APP_ID = "dev.tacticdev.easyclip"

# --- shared by the TACTICDEV Easy* tray apps -------------------------------------------------
# Only one tray panel open at a time: opening one tells the others (over the session bus) to close.
TRAY_IFACE, TRAY_PATH = "dev.tacticdev.Tray", "/dev/tacticdev/tray"

def tray_announce():
    try:
        Gio.bus_get_sync(Gio.BusType.SESSION, None).emit_signal(
            None, TRAY_PATH, TRAY_IFACE, "Opened", GLib.Variant("(s)", (APP_ID,)))
    except GLib.Error:
        pass

def tray_listen(hide):
    Gio.bus_get_sync(Gio.BusType.SESSION, None).signal_subscribe(
        None, TRAY_IFACE, "Opened", TRAY_PATH, None, Gio.DBusSignalFlags.NONE,
        lambda c, s, p, i, n, params: params.unpack()[0] != APP_ID and hide())

def add_restart_action(app):
    """Self-restart that stays inside the desktop session (system permissions depend on it).
    Trigger: gdbus call --session -d <APP_ID> -o /<APP_ID with dots as slashes>
             -m org.freedesktop.Application.ActivateAction restart [] {}"""
    act = Gio.SimpleAction.new("restart", None)
    def restart(*_):
        script = (f"while kill -0 {os.getpid()} 2>/dev/null; do sleep 0.2; done; "
                  f"exec {sys.executable} {os.path.abspath(__file__)} --hidden")
        subprocess.Popen(["sh", "-c", script])
        app.quit()
    act.connect("activate", restart)
    app.add_action(act)
# ---------------------------------------------------------------------------------------------
DATA_DIR = os.path.join(GLib.get_user_data_dir(), "easyclip")
IMG_DIR = os.path.join(DATA_DIR, "images")
HISTORY = os.path.join(DATA_DIR, "history.json")
CONFIG_DIR = os.path.join(GLib.get_user_config_dir(), "easyclip")
CONFIG = os.path.join(CONFIG_DIR, "config.json")
AUTOSTART = os.path.join(GLib.get_user_config_dir(), "autostart", "easyclip.desktop")
DEFAULTS = {"max_items": 25, "save_images": True, "auto_paste": True, "show_tray": True}
TRAY_ITEMS = 8
MAX_TEXT = 200_000          # characters kept per item
MAX_IMAGE_PIXELS = 4096 * 4096
TERMINALS = ("gnome-terminal", "xfce4-terminal", "tilix", "terminator", "kitty", "alacritty", "konsole",
             "xterm", "urxvt", "wezterm", "foot", "guake", "mate-terminal", "lxterminal", "ptyxis", "warp")
SECRET_TARGETS = ("x-kde-passwordManagerHint",)  # KeePassXC, Bitwarden & co. set this on secrets

CSS = b"""
#easyclip-list row { padding: 0; }
.clip-card { padding: 10px 12px; }
.clip-text { font-size: 10pt; }
.clip-meta { font-size: 8.5pt; color: alpha(currentColor, 0.55); }
.clip-section { font-weight: bold; font-size: 9pt; color: alpha(currentColor, 0.6); padding: 10px 14px 4px; }
.clip-tool { padding: 2px 4px; min-height: 0; min-width: 0; }
row.easyclip-last { border-bottom: 1px solid alpha(currentColor, 0.12); }
/* tray-panel look shared with EasySound / EasyNet / EasyPower */
window.easyclip { background-color: transparent; }
.ec-card { background-color: #2c2c2c; color: #ffffff; border: 1px solid rgba(255,255,255,0.08);
           border-radius: 18px; padding: 14px 14px 10px; }
.ec-title { font-weight: bold; font-size: 11pt; }
.ec-iconbtn { padding: 4px; min-width: 0; min-height: 0; border-radius: 10px; }
.ec-listwrap { background-color: rgba(255,255,255,0.05); border-radius: 14px; }
.ec-listwrap list, .ec-listwrap row { background-color: transparent; }
.ec-listwrap row:selected { background-color: rgba(75,144,237,0.18); }
.ec-listwrap row:hover { background-color: rgba(255,255,255,0.06); }
.ec-link { padding: 6px 10px; border-radius: 10px; }
"""


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default

def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)

def ago(ts):
    s = int(time.time() - ts)
    if s < 60: return "just now"
    if s < 3600: return f"{s // 60} min ago"
    if s < 86400: return f"{s // 3600} h ago"
    return time.strftime("%b %-d", time.localtime(ts))

def run(*cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=3).stdout.strip()
    except Exception:
        return ""


class Store:
    """History on disk: ~/.local/share/easyclip/history.json + images/*.png. Newest first."""
    def __init__(self, cfg):
        self.cfg = cfg
        self.items = load_json(HISTORY, [])
        os.makedirs(IMG_DIR, exist_ok=True)
        self._save_pending = False

    def save(self):
        if self._save_pending: return
        self._save_pending = True
        def do():
            self._save_pending = False
            save_json(HISTORY, self.items)
            return False
        GLib.timeout_add(400, do)

    def _key(self, kind, payload):
        return kind + ":" + hashlib.sha1(payload).hexdigest()

    def add(self, kind, text=None, pixbuf=None):
        """Add (or bump to the top) an item. Returns True if anything changed."""
        if kind == "text":
            text = text[:MAX_TEXT]
            key = self._key("text", text.encode())
        else:
            key = self._key("image", pixbuf.get_pixels())
        for it in self.items:
            if it["key"] == key:
                it["ts"] = time.time()
                self._to_top(it); self.save()
                return True
        item = {"id": uuid.uuid4().hex, "key": key, "kind": kind, "ts": time.time(), "pinned": False}
        if kind == "text":
            item["text"] = text
        else:
            path = os.path.join(IMG_DIR, item["id"] + ".png")
            pixbuf.savev(path, "png", [], [])
            item["image"] = path
            item["size"] = f"{pixbuf.get_width()}×{pixbuf.get_height()}"
        self.items.insert(0, item)
        self._trim(); self.save()
        return True

    def _to_top(self, it):
        self.items.remove(it); self.items.insert(0, it)

    def _trim(self):
        unpinned = [i for i in self.items if not i["pinned"]]
        for old in unpinned[self.cfg["max_items"]:]:
            self.remove(old, save=False)

    def remove(self, it, save=True):
        if it in self.items:
            self.items.remove(it)
        if it.get("image"):
            try: os.remove(it["image"])
            except OSError: pass
        if save: self.save()

    def clear(self):
        for it in [i for i in self.items if not i["pinned"]]:
            self.remove(it, save=False)
        self.save()


class Watcher:
    """Follows the CLIPBOARD selection and feeds new copies into the store."""
    def __init__(self, app):
        self.app = app
        self.clip = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        self.ignore_next = None    # key of something we put on the clipboard ourselves
        self.pending = None
        self.paused_until = 0
        self.clip.connect("owner-change", self._changed)

    def _changed(self, *_):
        # Apps often fire several changes in a row; read once things settle.
        if self.pending: GLib.source_remove(self.pending)
        self.pending = GLib.timeout_add(150, self._read)

    def _read(self):
        self.pending = None
        if time.time() < self.paused_until:
            return False
        self.clip.request_targets(self._got_targets)
        return False

    def _got_targets(self, clip, atoms, *_):
        names = [a.name() for a in (atoms or [])]
        if not names:
            # The app that owned the clipboard closed: put the last copy back so paste still works.
            self._restore_last()
            return
        if any(t in names for t in SECRET_TARGETS):
            return  # password-manager secret: never stored
        has_text = any(n in names for n in ("UTF8_STRING", "text/plain", "text/plain;charset=utf-8", "STRING", "TEXT"))
        has_image = any(n.startswith("image/") for n in names)
        if has_image and not has_text:
            if self.app.cfg["save_images"]:
                clip.request_image(self._got_image)
        elif has_text:
            clip.request_text(self._got_text)

    def _got_text(self, clip, text, *_):
        if not text or not text.strip():
            return
        key = "text:" + hashlib.sha1(text[:MAX_TEXT].encode()).hexdigest()
        if key == self.ignore_next:
            self.ignore_next = None
            return
        if self.app.store.add("text", text=text):
            self.app.refresh()

    def _got_image(self, clip, pixbuf, *_):
        if not pixbuf or pixbuf.get_width() * pixbuf.get_height() > MAX_IMAGE_PIXELS:
            return
        key = "image:" + hashlib.sha1(pixbuf.get_pixels()).hexdigest()
        if key == self.ignore_next:
            self.ignore_next = None
            return
        if self.app.store.add("image", pixbuf=pixbuf):
            self.app.refresh()

    def _restore_last(self):
        items = self.app.store.items
        if items:
            self.put(items[0], restoring=True)

    def put(self, it, restoring=False):
        """Place an item on the clipboard (and hand it to the clipboard manager so it survives)."""
        self.ignore_next = it["key"]
        if it["kind"] == "text":
            self.clip.set_text(it["text"], -1)
        else:
            try:
                self.clip.set_image(GdkPixbuf.Pixbuf.new_from_file(it["image"]))
            except GLib.Error:
                return False
        self.clip.store()
        return True


class Row(Gtk.ListBoxRow):
    def __init__(self, app, it):
        super().__init__()
        self.app, self.item = app, it
        card = Gtk.Box(spacing=10); card.get_style_context().add_class("clip-card")
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        if it["kind"] == "text":
            preview = " ".join(it["text"].split())  # one tidy line of the start of the text
            lbl = Gtk.Label(label=preview[:300], xalign=0, lines=2, wrap=True,
                            wrap_mode=Pango.WrapMode.WORD_CHAR, ellipsize=Pango.EllipsizeMode.END,
                            max_width_chars=40)
            lbl.get_style_context().add_class("clip-text")
            lbl.set_tooltip_text(it["text"][:1500])
            col.pack_start(lbl, False, False, 0)
            meta_kind = f"{len(it['text'])} characters" if len(it["text"]) > 120 else ""
        else:
            img = Gtk.Image(xalign=0)
            try:
                pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(it["image"], 300, 90, True)
                img.set_from_pixbuf(pb)
            except GLib.Error:
                img.set_from_icon_name("image-missing", Gtk.IconSize.DIALOG)
            col.pack_start(img, False, False, 0)
            meta_kind = f"Image {it.get('size', '')}"
        meta = Gtk.Label(label=" · ".join(x for x in (ago(it["ts"]), meta_kind) if x), xalign=0)
        meta.get_style_context().add_class("clip-meta")
        col.pack_start(meta, False, False, 0)
        card.pack_start(col, True, True, 0)
        tools = Gtk.Box(spacing=2, valign=Gtk.Align.START)
        pin = Gtk.Button(relief=Gtk.ReliefStyle.NONE, tooltip_text="Unpin" if it["pinned"] else "Pin (keep at the top)")
        pin.add(Gtk.Image.new_from_icon_name("starred-symbolic" if it["pinned"] else "non-starred-symbolic",
                                             Gtk.IconSize.BUTTON))
        pin.get_style_context().add_class("clip-tool")
        pin.connect("clicked", lambda *_: app.toggle_pin(it))
        rm = Gtk.Button(relief=Gtk.ReliefStyle.NONE, tooltip_text="Delete")
        rm.add(Gtk.Image.new_from_icon_name("user-trash-symbolic", Gtk.IconSize.BUTTON))
        rm.get_style_context().add_class("clip-tool")
        rm.connect("clicked", lambda *_: app.delete(it))
        tools.pack_start(pin, False, False, 0); tools.pack_start(rm, False, False, 0)
        card.pack_end(tools, False, False, 0)
        self.add(card)

    def matches(self, q):
        if not q: return True
        if self.item["kind"] == "text":
            return q in self.item["text"].lower()
        return "image" in q


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.win = None
        self.prev_window = None

    # ---- startup / command line ----
    def do_startup(self):
        Gtk.Application.do_startup(self)
        self.hold()  # keep watching the clipboard with no window open
        self.cfg = dict(DEFAULTS, **load_json(CONFIG, {}))
        self.store = Store(self.cfg)
        self.watcher = Watcher(self)
        css = Gtk.CssProvider(); css.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.tray = None
        self.build()
        self.setup_tray()
        tray_listen(lambda: self.win.get_visible() and self.win.hide())
        add_restart_action(self)
        GLib.timeout_add_seconds(30, lambda: (self.update_tray(), True)[-1])  # keep "paused" state fresh

    # ---- tray icon ----
    def setup_tray(self):
        if XApp is None:
            return
        self.tray = XApp.StatusIcon()
        self.tray.set_name("easyclip")
        self.tray.connect("activate", lambda *_: self.toggle())
        self.tray_menu = Gtk.Menu()
        self.tray.set_secondary_menu(self.tray_menu)
        self.tray.set_visible(self.cfg["show_tray"])
        self.update_tray()

    def update_tray(self):
        if not self.tray:
            return
        n = len(self.store.items)
        paused = time.time() < self.watcher.paused_until
        self.tray.set_icon_name("edit-paste-symbolic")
        self.tray.set_tooltip_text(
            ("Clipboard paused · " if paused else "Clipboard · ") +
            (f"{n} item{'s' if n != 1 else ''}" if n else "empty") + " · Super+V")
        for c in self.tray_menu.get_children(): c.destroy()
        def item(label, cb, sensitive=True):
            it = Gtk.MenuItem(label=label); it.set_sensitive(sensitive)
            it.connect("activate", lambda *_: cb()); self.tray_menu.append(it)
        items = sorted(self.store.items, key=lambda i: (not i["pinned"],))[:TRAY_ITEMS]
        if not items:
            item("Nothing copied yet", lambda: None, False)
        for it in items:
            if it["kind"] == "text":
                t = " ".join(it["text"].split())
                label = (t[:48] + "…") if len(t) > 48 else t
            else:
                label = f"Image {it.get('size', '')}"
            item(("★ " if it["pinned"] else "") + label, lambda it=it: self.paste_from_tray(it))
        self.tray_menu.append(Gtk.SeparatorMenuItem())
        item("Open clipboard   (Super+V)", self.toggle)
        if paused:
            item("Resume saving copies", self.resume)
        else:
            item("Pause for 5 minutes", self.pause)
        item("Clear history…", lambda: (self.toggle() if not self.win.get_visible() else None, self.confirm_clear()),
             any(not i["pinned"] for i in self.store.items))
        self.tray_menu.append(Gtk.SeparatorMenuItem())
        item("Quit", self.quit)
        self.tray_menu.show_all()

    def paste_from_tray(self, it):
        # The tray menu doesn't take window focus, so the active window is where the user was.
        self.prev_window = run("xdotool", "getactivewindow") or None
        self.paste(it)

    def pause(self):
        self.watcher.paused_until = time.time() + 300
        self.flash("Paused for 5 minutes"); self.update_tray()
        GLib.timeout_add_seconds(301, lambda: (self.update_tray(), False)[-1])

    def resume(self):
        self.watcher.paused_until = 0
        self.flash("Saving copies again"); self.update_tray()

    def do_command_line(self, cl):
        args = cl.get_arguments()[1:]
        if "--version" in args:
            cl.print_literal(f"EasyClip {VERSION}\n"); return 0
        if "--quit" in args:
            self.quit(); return 0
        if "--hidden" in args:
            return 0
        self.toggle()  # --show, or launched from the menu
        return 0

    # ---- window ----
    def build(self):
        # Same rounded tray panel as EasySound/EasyNet/EasyPower. Kept as a (borderless) dialog rather
        # than a popup-menu window so the search box gets keyboard focus.
        w = Gtk.ApplicationWindow(application=self, title="Clipboard", decorated=False, resizable=False)
        w.get_style_context().add_class("easyclip")
        vis = w.get_screen().get_rgba_visual()
        if vis: w.set_visual(vis)
        w.set_app_paintable(True)
        w.set_icon_name("edit-paste")
        w.set_keep_above(True); w.set_skip_taskbar_hint(True); w.set_skip_pager_hint(True)
        w.set_type_hint(Gdk.WindowTypeHint.DIALOG)
        w.connect("delete-event", lambda *_: (w.hide(), True)[-1])
        w.connect("key-press-event", self.on_key)
        w.connect("focus-out-event", self.on_focus_out)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin=8)
        box.get_style_context().add_class("ec-card"); box.set_size_request(380, -1)
        head = Gtk.Box(spacing=8)
        t = Gtk.Label(label="Clipboard", xalign=0); t.get_style_context().add_class("ec-title")
        gear = Gtk.MenuButton(relief=Gtk.ReliefStyle.NONE, tooltip_text="Settings")
        gear.get_style_context().add_class("ec-iconbtn")
        gear.add(Gtk.Image.new_from_icon_name("emblem-system-symbolic", Gtk.IconSize.BUTTON))
        gear.set_popover(self.settings_popover(gear))
        head.pack_start(t, True, True, 0); head.pack_end(gear, False, False, 0)
        box.pack_start(head, False, False, 0)
        self.search = Gtk.SearchEntry(placeholder_text="Search")
        self.search.connect("search-changed", lambda *_: self.apply_filter())
        self.search.connect("activate", lambda *_: self.paste_first())
        box.pack_start(self.search, False, False, 0)
        sc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, min_content_height=380, max_content_height=380)
        sc.get_style_context().add_class("ec-listwrap")
        frame = Gtk.Box()
        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.BROWSE, activate_on_single_click=True)
        self.list.set_name("easyclip-list")
        self.list.connect("row-activated", lambda _l, row: self.paste(row.item))
        self.list.set_filter_func(lambda row: row.matches(self.search.get_text().strip().lower()))
        self.list.set_header_func(self._header)
        self.empty = Gtk.Label(label="Nothing copied yet.\nCopy some text or an image and it shows up here.",
                               justify=Gtk.Justification.CENTER, margin=40)
        self.empty.get_style_context().add_class("dim-label")
        self.list.set_placeholder(self.empty)
        frame.pack_start(self.list, True, True, 0); sc.add(frame)
        box.pack_start(sc, True, True, 0)
        foot = Gtk.Box(spacing=8)
        self.hint = Gtk.Label(xalign=0); self.hint.get_style_context().add_class("dim-label")
        self.hint.set_markup("<small>Enter or click pastes · Esc closes</small>")
        clear = Gtk.Button(label="Clear history", relief=Gtk.ReliefStyle.NONE)
        clear.get_style_context().add_class("ec-link"); clear.connect("clicked", lambda *_: self.confirm_clear())
        foot.pack_start(self.hint, True, True, 0); foot.pack_end(clear, False, False, 0)
        box.pack_start(foot, False, False, 0)
        w.add(box)
        self.win = w
        self.refresh()

    def _header(self, row, before):
        title = None
        if before is None:
            title = "Pinned" if row.item["pinned"] else "Recent"
        elif before.item["pinned"] and not row.item["pinned"]:
            title = "Recent"
        if title:
            lbl = Gtk.Label(label=title, xalign=0); lbl.get_style_context().add_class("clip-section")
            row.set_header(lbl)
        elif before is not None:
            row.set_header(Gtk.Separator())
        else:
            row.set_header(None)

    def settings_popover(self, relative):
        pop = Gtk.Popover(relative_to=relative)
        g = Gtk.Grid(row_spacing=10, column_spacing=16, margin=14)
        def sw_row(r, text, sub, active, cb):
            t = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            t.pack_start(Gtk.Label(label=text, xalign=0), False, False, 0)
            if sub:
                s = Gtk.Label(xalign=0, wrap=True, max_width_chars=34); s.set_markup(f"<small>{sub}</small>")
                s.get_style_context().add_class("dim-label"); t.pack_start(s, False, False, 0)
            sw = Gtk.Switch(active=active, valign=Gtk.Align.CENTER)
            sw.connect("notify::active", lambda s_, _p: cb(s_.get_active()))
            g.attach(t, 0, r, 1, 1); g.attach(sw, 1, r, 1, 1)
        sw_row(0, "Paste right away", "Off: clicking only copies, then you paste yourself.",
               self.cfg["auto_paste"], lambda on: self.set_cfg("auto_paste", on))
        sw_row(1, "Keep images", None, self.cfg["save_images"], lambda on: self.set_cfg("save_images", on))
        sw_row(2, "Start at login", "Needed for history to keep filling up.", os.path.exists(AUTOSTART), self.set_autostart)
        sw_row(3, "Tray icon", "Right-click it for recent items.", self.cfg["show_tray"],
               lambda on: (self.set_cfg("show_tray", on), self.tray and self.tray.set_visible(on)))
        g.attach(Gtk.Label(label="Remember", xalign=0), 0, 4, 1, 1)
        spin = Gtk.SpinButton.new_with_range(5, 500, 5); spin.set_value(self.cfg["max_items"])
        spin.connect("value-changed", lambda s: (self.set_cfg("max_items", int(s.get_value())), self.store._trim(), self.refresh()))
        g.attach(spin, 1, 4, 1, 1)
        pause = Gtk.Button(label="Pause for 5 minutes")
        pause.set_tooltip_text("Don't save anything you copy for the next 5 minutes")
        pause.connect("clicked", lambda *_: (self.pause(), pop.popdown()))
        g.attach(pause, 0, 5, 2, 1)
        ver = Gtk.Label(xalign=0); ver.set_markup(f"<small>EasyClip {VERSION} · Super+V opens this panel</small>")
        ver.get_style_context().add_class("dim-label")
        g.attach(ver, 0, 6, 2, 1)
        g.show_all(); pop.add(g)
        return pop

    # ---- show / hide ----
    def toggle(self):
        if self.win.get_visible():
            self.win.hide(); return
        tray_announce()
        # Remember where the user was so we can paste back into it.
        self.prev_window = run("xdotool", "getactivewindow") or None
        self.refresh()
        self.search.set_text("")
        self.win.show_all()
        self.place_near_pointer()
        self.win.present_with_time(Gtk.get_current_event_time() or int(time.time()))
        self.search.grab_focus()
        first = self.list.get_row_at_index(0)
        if first: self.list.select_row(first)

    def place_near_pointer(self):
        disp = Gdk.Display.get_default()
        _s, x, y = disp.get_default_seat().get_pointer().get_position()
        mon = disp.get_monitor_at_point(x, y).get_workarea()
        ww, wh = self.win.get_size()
        nx = min(max(mon.x + 8, x - ww // 2), mon.x + mon.width - ww - 8)
        ny = y - wh - 12 if y - wh - 12 > mon.y else y + 12
        self.win.move(nx, min(max(mon.y + 8, ny), mon.y + mon.height - wh - 8))

    def on_focus_out(self, *_):
        # Behave like a popup: clicking elsewhere closes it (but not while the settings popover is open).
        def maybe_hide():
            if self.win.get_visible() and not self.win.is_active() and not self._popover_open():
                self.win.hide()
            return False
        GLib.timeout_add(150, maybe_hide)
        return False

    def _popover_open(self):
        return any(isinstance(w, Gtk.Popover) and w.get_visible() for w in Gtk.Window.list_toplevels()) or \
               bool(getattr(self, "_dialog_open", False))

    def on_key(self, w, ev):
        k = ev.keyval
        if k == Gdk.KEY_Escape:
            if self.search.get_text(): self.search.set_text("")
            else: w.hide()
            return True
        if k in (Gdk.KEY_Down, Gdk.KEY_Up) and self.search.has_focus():
            rows = [r for r in self.list.get_children() if r.get_child_visible()]
            if rows:
                sel = self.list.get_selected_row()
                i = rows.index(sel) if sel in rows else -1
                i = min(len(rows) - 1, i + 1) if k == Gdk.KEY_Down else max(0, i - 1)
                self.list.select_row(rows[i]); rows[i].grab_focus()
            return True
        if k == Gdk.KEY_Delete and not self.search.has_focus():
            sel = self.list.get_selected_row()
            if sel: self.delete(sel.item)
            return True
        # Typing anywhere goes to the search box.
        if not self.search.has_focus() and ev.string and ev.string.isprintable() and not (ev.state & Gdk.ModifierType.CONTROL_MASK):
            self.search.grab_focus_without_selecting(); self.search.set_position(-1)
        return False

    # ---- list ----
    def refresh(self):
        if not self.win: return
        for r in self.list.get_children(): r.destroy()
        items = sorted(self.store.items, key=lambda i: (not i["pinned"],))  # pinned first, keep recency order
        for it in items:
            self.list.add(Row(self, it))
        self.list.show_all()
        self.apply_filter()
        self.update_tray()

    def apply_filter(self):
        self.list.invalidate_filter()
        q = self.search.get_text().strip()
        self.empty.set_text(f"Nothing matches “{q}”." if q else
                            "Nothing copied yet.\nCopy some text or an image and it shows up here.")
        visible = [r for r in self.list.get_children() if r.get_child_visible()]
        for r in self.list.get_children():
            ctx = r.get_style_context()
            (ctx.add_class if visible and r is visible[-1] else ctx.remove_class)("easyclip-last")
        if visible and self.list.get_selected_row() not in visible:
            self.list.select_row(visible[0])

    # ---- actions ----
    def paste_first(self):
        sel = self.list.get_selected_row()
        rows = [r for r in self.list.get_children() if r.get_child_visible()]
        target = sel if sel in rows else (rows[0] if rows else None)
        if target: self.paste(target.item)

    def paste(self, it):
        if not self.watcher.put(it):
            self.flash("That image is gone."); return
        if not it["pinned"]:
            it["ts"] = time.time()
            self.store._to_top(it); self.store.save()
        self.win.hide()
        prev = self.prev_window
        if self.cfg["auto_paste"] and prev:
            threading.Thread(target=self._send_paste, args=(prev,), daemon=True).start()
        GLib.timeout_add(300, lambda: (self.refresh(), False)[-1])

    def _send_paste(self, wid):
        time.sleep(0.12)
        wm_class = run("xprop", "-id", wid, "WM_CLASS").lower()
        combo = "ctrl+shift+v" if any(t in wm_class for t in TERMINALS) else "ctrl+v"
        run("xdotool", "windowactivate", "--sync", wid)
        time.sleep(0.05)
        run("xdotool", "key", "--clearmodifiers", combo)

    def toggle_pin(self, it):
        it["pinned"] = not it["pinned"]
        self.store.save(); self.refresh()

    def delete(self, it):
        self.store.remove(it); self.refresh()

    def confirm_clear(self):
        self._dialog_open = True
        d = Gtk.MessageDialog(transient_for=self.win, modal=True, message_type=Gtk.MessageType.QUESTION,
                              buttons=Gtk.ButtonsType.NONE, text="Clear clipboard history?")
        d.format_secondary_text("Pinned items are kept.")
        d.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Clear", Gtk.ResponseType.OK)
        d.get_widget_for_response(Gtk.ResponseType.OK).get_style_context().add_class("destructive-action")
        if d.run() == Gtk.ResponseType.OK:
            self.store.clear(); self.refresh()
        d.destroy(); self._dialog_open = False
        self.win.present()

    def flash(self, msg):
        self.hint.set_markup(f"<small>{GLib.markup_escape_text(msg)}</small>")
        GLib.timeout_add_seconds(4, lambda: (self.hint.set_markup("<small>Enter or click pastes · Esc closes</small>"), False)[-1])

    # ---- settings ----
    def set_cfg(self, k, v):
        self.cfg[k] = v; save_json(CONFIG, self.cfg)

    def set_autostart(self, on):
        if on:
            os.makedirs(os.path.dirname(AUTOSTART), exist_ok=True)
            launcher = shutil.which("easyclip") or f"/usr/bin/python3 {os.path.abspath(__file__)}"
            with open(AUTOSTART, "w") as f:
                f.write("[Desktop Entry]\nType=Application\nName=EasyClip\nComment=Clipboard history (Super+V)\n"
                        f"Exec={launcher} --hidden\nIcon=edit-paste\nTerminal=false\nX-GNOME-Autostart-enabled=true\n")
        elif os.path.exists(AUTOSTART):
            os.remove(AUTOSTART)


if __name__ == "__main__":
    sys.exit(App().run(sys.argv))
