"""CLIP STUDIO PAINT 1.13.2 API, called in-process via ctypes. See RE_NOTES.md for each address.

    doc = csp.active_document()
    layer = doc.new_raster_layer("ink")      # undoable
    csp.run("Layer/Duplicate Layer")          # any main-menu command, like clicking it; see csp.commands()

Everything here must run on CSP's UI thread (console code and Scripts-menu scripts already do).
Raw memory helpers (read, va, rtti_name, ...) live in mem.py.
"""
import contextlib
import ctypes
import enum
import os
import struct
import sys
import tempfile
import zlib
from ctypes import c_double, c_int32, c_uint32, c_uint64, c_void_p

import mem
from mem import read_u64 as _u64, va as _va


class CspError(RuntimeError):
    """CSP refused or couldn't do what was asked (no canvas, command disabled, layer gone, ...)."""


def _fn(static_va, restype, *argtypes):
    return ctypes.CFUNCTYPE(restype, *argtypes)(_va(static_va))


# --- app: URApplication owns the main window, its menu and the open documents
_VT_URApplication = _va(0x143227F60)
_APP_SLOT = _va(0x143D81A60)  # a static boost::function bound to the app (its `this`), filled at startup
_DOC_CLASS = ".?AVPWCanvasModel@Planeswalker@@"


def _app():
    tid = getattr(sys.modules.get("startup"), "UI_TID", 0)
    if tid and mem.kernel32.GetCurrentThreadId() != tid:
        raise CspError("csp must run on CSP's UI thread (console code and menu scripts already do)")
    app = _u64(_APP_SLOT)
    if not app or _u64(app) != _VT_URApplication:
        raise CspError("CSP's application object not found (still starting up?)")
    return app


# lock inc dword [rcx+8] ; ret  -- +1 on a boost shared_ptr use count. Atomic: CSP's worker threads share docs.
_stub = mem.kernel32.VirtualAlloc(None, 16, 0x3000, 0x40)
ctypes.memmove(_stub, b"\xf0\xff\x41\x08\xc3", 5)
_addref = ctypes.CFUNCTYPE(None, c_void_p)(_stub)
_sp_release = _fn(0x14013B530, None, c_void_p)
# undo history (PWDoExecutor = the document)
_self_sp = _fn(0x141208F20, c_void_p, c_void_p, c_void_p)  # (obj, out) -> sp from obj's weak self at +0x28 (+1)
_cmd_attach = _fn(0x141375950, None, c_void_p, c_void_p)  # (cmd, sp<executor> by value)
_cmd_set_label = _fn(0x141235AA0, None, c_void_p, c_void_p)  # (cmd, PWString*): the step's name
_multi_new = _fn(0x1414E1BA0, c_void_p, c_void_p, c_int32)  # (out sp<PWDoMultiCommand>*, 1)
_multi_add = _fn(0x1414E1B00, None, c_void_p, c_void_p)  # (multi, sp<cmd> by value): appends a child
_history_record = _fn(0x1415D33E0, None, c_void_p, c_void_p)  # (history, sp<cmd> by value): drop redo, append
_history_notify = _fn(0x1413752B0, None, c_void_p, c_int32, c_int32, c_void_p)  # (executor, 1, 1, sp<cmd> by value)
_node_free = _fn(0x1417F3790, None, c_void_p)  # CSP's operator delete for list nodes


class Document:
    """An open canvas (PWCanvasModel). Holds no reference: it raises CspError once the canvas is closed."""

    def __init__(self, ptr):
        self.ptr = ptr

    def _sp(self):
        """A fresh shared_ptr<Doc> {ptr, ctrl} holding +1 ref; callees taking it by value consume it."""
        for ptr, ctrl in mem.sp_vector(_app() + 0x300):
            if ptr == self.ptr and ctrl:
                _addref(ctrl)
                return (c_uint64 * 2)(ptr, ctrl)
        raise CspError("document is closed")

    @property
    def active_layer(self):
        out, doc = (c_uint64 * 2)(), self._sp()  # keep doc's buffer alive across the call
        _current_layer(ctypes.addressof(out), ctypes.addressof(doc))
        ptr = out[0]
        _sp_release(ctypes.addressof(out))  # the document keeps the layer alive
        if not ptr:
            raise CspError("no current layer")
        return Layer(ptr, self)

    @active_layer.setter
    def active_layer(self, layer):
        """Select `layer` (only it), like clicking it in the Layer palette."""
        if layer.document != self:
            raise CspError(f"{layer!r} belongs to another document")
        doc, sp = self._sp(), layer._sp()
        _select_layer(ctypes.addressof(doc), ctypes.addressof(sp), 0, 0)

    @property
    def layers(self):
        """Top-level layers, top to bottom like the Layer palette (see Layer.children)."""
        layer = self.active_layer
        while (up := _u64(layer.ptr + 0x1C0)) and _u64(up + 0x1C0):  # stop below the unnamed root folder
            layer = Layer(up, self)
        return Layer(_u64(layer.ptr + 0x1C0), self).children

    def _create(self, handler, name):
        before = self.active_layer
        doc = self._sp()
        handler(ctypes.addressof(doc))
        layer = self.active_layer
        if layer == before:
            raise CspError("CSP did not create the layer")
        if name is not None:
            layer.name = name
        return layer

    @contextlib.contextmanager
    def undo_group(self, label="Script"):
        """Make everything done to this document inside the with-block one undo step named `label`
        (kept as one step even if the block raises). Steps undone inside the block are left as they are."""
        hist = self._history()
        start = _u64(_u64(hist + 0x20) + 8)  # node before the current position: the last done step (or head)
        start_cmd = _u64(start + 0x10)
        try:
            yield
        finally:
            self._collapse(hist, start, start_cmd, label)

    def _history(self):
        # PWDoExecutor: +0x58 weak parent executor (then the parent keeps the history), +0x70 sp<history>.
        # history: std::list of {next, prev, sp<PWDoCommand>}: +0 head, +8 size, +0x18 redo mark, +0x20 current
        # (one past the last done step).
        if _u64(self.ptr + 0x58):
            raise CspError("this document's undo history lives elsewhere")
        hist = _u64(self.ptr + 0x70)
        if not hist or not _u64(hist):
            raise CspError("document has no undo history")
        return hist

    def _collapse(self, hist, start, start_cmd, label):
        head, cur = _u64(hist), _u64(hist + 0x20)
        nodes, n = [], _u64(cur + 8)
        while n != start:
            if n == head or len(nodes) > 100000:
                return  # start was trimmed off or undone: leave the steps alone
            nodes.append(n)
            n = _u64(n + 8)
        if start != head and _u64(start + 0x10) != start_cmd:
            return  # a different node now sits at start's address
        if len(nodes) < 2:
            return
        nodes.reverse()
        c_uint64.from_address(start).value = cur  # unlink them: start.next = cur, cur.prev = start
        c_uint64.from_address(cur + 8).value = start
        c_uint64.from_address(hist + 8).value -= len(nodes)
        multi = (c_uint64 * 2)()
        _multi_new(ctypes.addressof(multi), 1)
        for node in nodes:
            sp = (c_uint64 * 2)(_u64(node + 0x10), _u64(node + 0x18))  # hand the node's reference to the group
            _multi_add(multi[0], ctypes.addressof(sp))
            _node_free(node)
        s = _pws_new(label)
        try:
            _cmd_set_label(multi[0], ctypes.addressof(s))
        finally:
            _pws_free(ctypes.addressof(s))
        owner = (c_uint64 * 2)()
        _self_sp(self.ptr, ctypes.addressof(owner))
        _cmd_attach(multi[0], ctypes.addressof(owner))  # what pushing a command does first (consumes owner)
        note = (c_uint64 * 2)(multi[0], multi[1])
        _addref(multi[1])
        _history_record(hist, ctypes.addressof(multi))  # records without running it again (consumes multi)
        _history_notify(self.ptr, 1, 1, ctypes.addressof(note))  # history palette / menus (consumes note)

    def _canvas(self):
        self._sp()  # raises once the document is closed
        return _u64(_u64(self.ptr + 0x100) + 0xA0)  # PWClipProject -> PWCanvas

    @property
    def width(self):
        """Canvas width in pixels."""
        return round(c_double.from_address(self._canvas() + 0xC8).value)

    @property
    def height(self):
        return round(c_double.from_address(self._canvas() + 0xD8).value)

    @property
    def resolution(self):
        """Dots per inch."""
        return c_double.from_address(self._canvas() + 0xE8).value

    def _tab(self):
        # shortcut: tabs are matched to documents by position (both in tab order); find a direct link if
        # tab dragging or split canvas windows ever break that.
        docs = [ptr for ptr, _ in mem.sp_vector(_app() + 0x300)]
        views = mem.sp_vector(_app() + 0x748)  # the canvases' PWScrollViews
        container = _u64(views[0][0] + 0x28) if views else None
        bar = _u64(container + 0x280) if container else None
        if not bar or mem.rtti_name(bar) != ".?AVPWWorkTabBar@Planeswalker@@":
            raise CspError("canvas tab bar not found")
        tabs = [ptr for ptr, _ in mem.sp_vector(bar + 0x278)]
        if self.ptr not in docs:
            raise CspError("document is closed")
        if len(tabs) != len(docs):
            raise CspError("canvas tabs don't line up with the open documents")
        return tabs[docs.index(self.ptr)]

    @property
    def name(self):
        """The name on the canvas tab (file name without extension, or e.g. "Illustration" if never saved)."""
        return _pws_read(self._tab() + 0x250)

    @property
    def modified(self):
        """Unsaved changes (the tab shows a *)."""
        tab = self._tab()
        return _pws_read(tab + 0x300) == _pws_read(tab + 0x250) + "*"

    def _run(self, command):
        """Run a menu command on this canvas (menu commands act on the active one), return the active layer."""
        if active_document() != self:
            raise CspError(f"{self!r} is not the active canvas")
        run(command)
        return self.active_layer

    def walk(self):
        """Every layer and folder, depth first, top to bottom like the Layer palette."""
        stack = self.layers[::-1]
        while stack:
            layer = stack.pop()
            yield layer
            stack.extend(layer.children[::-1])

    def find(self, name):
        """The first layer or folder named `name` (anywhere in the tree), or None."""
        return next((layer for layer in self.walk() if layer.name == name), None)

    def flatten(self):
        """Layer > Flatten image. Returns the resulting layer."""
        return self._run("Layer/Flatten image")

    def merge_visible(self):
        """Layer > Merge visible layers. Returns the resulting layer."""
        return self._run("Layer/Merge visible layers")

    def save(self):
        """File > Save (a never-saved canvas opens the Save dialog)."""
        self._run("File/Save")

    def save_as(self, path):
        """File > Save as `path` (.clip) without the dialog; the canvas is then that file."""
        path = os.path.abspath(path)
        if os.path.splitext(path)[1].lower() != ".clip":
            raise CspError(f"save_as writes .clip files; use export() for {path!r}")
        if not os.path.isdir(os.path.dirname(path)):
            raise CspError(f"folder {os.path.dirname(path)!r} doesn't exist")
        before = os.stat(path).st_mtime_ns if os.path.exists(path) else None
        hooks = _export_hooks()
        hooks["path"] = path
        try:
            self._run("File/Save as")
        finally:
            hooks["path"] = None
        if not os.path.exists(path) or os.stat(path).st_mtime_ns == before:
            raise CspError(f"CSP did not write {path!r}")

    def close(self, discard=False):
        """File > Close. A canvas with unsaved changes raises CspError unless discard=True, which answers
        CSP's "save changes?" prompt with Don't save."""
        if self.modified and not discard:
            raise CspError(f"{self.name!r} has unsaved changes (save() first, or close(discard=True))")
        if active_document() != self:
            raise CspError(f"{self!r} is not the active canvas")
        hooks = _message_hooks()
        hooks["answer"] = {5: _DONT_SAVE, 6: _DONT_SAVE}  # the Save / Don't save / Cancel button sets
        try:
            run("File/Close")  # not _run: there's no active layer to return afterwards
        finally:
            hooks["answer"] = None
        if self.ptr in [ptr for ptr, _ in mem.sp_vector(_app() + 0x300)]:
            raise CspError(f"CSP did not close {self!r}")

    def place(self, path):
        """File > Import > Image: `path` as a new layer above the current one. Returns that layer."""
        if active_document() != self:
            raise CspError(f"{self!r} is not the active canvas")
        _run_with_file("File/Import/Image", path)
        return self.active_layer

    def select_all(self):
        self._run("Select/Select All")

    def select_rect(self, left, top, right, bottom):
        """Replace the selection with this rectangle (canvas pixels; right/bottom exclusive). One undo step."""
        if not (left < right and top < bottom):
            raise CspError(f"empty rectangle {(left, top, right, bottom)}")
        if active_document() != self:
            raise CspError(f"{self!r} is not the active canvas")
        _call_virtual(self.ptr, 0x48)  # what Select All does first (ends whatever the canvas was doing)
        area = (c_uint64 * 2)()
        doc = self._sp()
        _select_area(ctypes.addressof(area), ctypes.addressof(doc))
        try:
            # Select All's recipe (140481da0 -> 140510000 -> 141aa5ff0) with our rectangle.
            cmd, snap = (c_uint64 * 2)(), (c_uint64 * 2)(area[0], area[1])
            _addref(area[1])
            _select_cmd(ctypes.addressof(cmd), ctypes.addressof(snap), 0)  # undo snapshot of the selection
            label = _pws_new("Select rectangle")
            try:
                _cmd_set_label(cmd[0], ctypes.addressof(label))
            finally:
                _pws_free(ctypes.addressof(label))
            _area_reset(area[0], self.width, self.height, 1)  # empty mask, canvas sized
            mask = (c_uint64 * 2)()
            _get_raster(_u64(area[0] + 8), ctypes.addressof(mask))
            try:
                bounds = (c_int32 * 4).from_address(area[0] + 0x18)
                bounds[:] = [left, top, right, bottom]
                value = ctypes.create_string_buffer(8)
                _value_init(value)
                _value_set(value, 0xFF, 0xFF)
                _fill_rect(mask[0], ctypes.addressof(bounds), value, 0)  # clipped to the canvas
                c_int32.from_address(area[0] + 0x28).value = 2  # "has a selection", as Select All sets
            finally:
                _sp_release(ctypes.addressof(mask))
            _push(self.ptr, ctypes.addressof(cmd))
        finally:
            _sp_release(ctypes.addressof(area))
        for refresh in (_selection_changed, _selection_changed2):
            doc = self._sp()
            refresh(ctypes.addressof(doc))

    def export(self, path):
        """File > Export (Single Layer) to `path` without any dialog. The format comes from the extension
        (.png .jpg .bmp .tif .tga .psd .psb); CSP uses the last export settings for that format, without the
        preview. An existing file is overwritten."""
        path = os.path.abspath(path)
        item = _EXPORT_MENU.get(os.path.splitext(path)[1].lower())
        if not item:
            raise CspError(f"can't export to {path!r}: use one of {sorted(_EXPORT_MENU)}")
        if not os.path.isdir(os.path.dirname(path)):
            raise CspError(f"folder {os.path.dirname(path)!r} doesn't exist")
        before = os.stat(path).st_mtime_ns if os.path.exists(path) else None
        hooks = _export_hooks()
        hooks["path"] = path
        try:
            self._run("File/Export (Single Layer)/" + item)
        finally:
            hooks["path"] = None
        if not os.path.exists(path) or os.stat(path).st_mtime_ns == before:
            raise CspError(f"CSP did not write {path!r}")

    def image(self):
        """The canvas as you see it (visible layers merged) as an Image."""
        # shortcut: goes through a temporary .tga export (CSP's last Targa settings); read the layer rasters
        # directly if that gets too slow or a setting gets in the way.
        with tempfile.TemporaryDirectory(prefix="cspy-") as folder:
            path = os.path.join(folder, "canvas.tga")
            self.export(path)
            image = _read_tga(path)
        if (image.width, image.height) != (self.width, self.height):
            raise CspError(f"the Targa export settings scale the image to {image.width} x {image.height}; "
                           "export a .tga once by hand at 100% and try again")
        return image

    def fill(self, rgb=None):
        """Edit > Fill: fill the selection (whole layer without one) on the current layer with the main
        color, or with `rgb` (the main color is put back afterwards)."""
        if rgb is None:
            return self._run("Edit/Fill")
        old = main_color()
        set_main_color(rgb)
        try:
            self._run("Edit/Fill")
        finally:
            set_main_color(old)

    def deselect(self):
        self._run("Select/Deselect")

    def _selection_bounds(self):
        """The box CSP keeps around the selection (left, top, right, bottom), or None without one. Exact right
        after Create Selection; after an undo CSP may widen it to the whole canvas."""
        area, doc = (c_uint64 * 2)(), self._sp()
        _select_area(ctypes.addressof(area), ctypes.addressof(doc))
        try:
            if not c_int32.from_address(area[0] + 0x28).value:  # state: 0 = nothing selected
                return None
            return tuple((c_int32 * 4).from_address(area[0] + 0x18))
        finally:
            _sp_release(ctypes.addressof(area))

    def crop(self):
        """Edit > Crop: crop the canvas to the selection."""
        self._run("Edit/Crop")

    _ROTATE = {90: "Rotate by 90 Degrees Clockwise", 180: "Rotate 180", -90: "Rotate by 90 Degrees Counterclockwise",
               270: "Rotate by 90 Degrees Counterclockwise"}

    def rotate(self, degrees):
        """Rotate the canvas clockwise by 90, 180 or -90 (= 270)."""
        if degrees not in self._ROTATE:
            raise CspError(f"rotate() takes one of {list(self._ROTATE)}, not {degrees!r}")
        self._run("Edit/Rotate/Flip canvas/" + self._ROTATE[degrees])

    def flip(self, direction="horizontal"):
        """Flip the whole canvas: "horizontal" or "vertical"."""
        self._run("Edit/Rotate/Flip canvas/" + _flip_name(direction))

    def copy(self):
        """Edit > Copy (the selection on the current layer)."""
        self._run("Edit/Copy")

    def cut(self):
        self._run("Edit/Cut")

    def paste(self):
        """Edit > Paste: the clipboard as a new layer. Returns it."""
        return self._run("Edit/Paste")

    def invert_selection(self):
        self._run("Select/Invert selected area")

    def new_raster_layer(self, name=None):
        """Same as Layer > New Raster Layer (undoable). Returns the new layer."""
        return self._create(_new_raster_layer, name)

    def new_folder(self, name=None):
        """Same as Layer > New Layer Folder (undoable). Returns the new folder."""
        return self._create(_new_folder, name)

    def new_vector_layer(self, name=None):
        """A new vector layer above the current one, like New Raster Layer but vector (undoable)."""
        return self._create(_new_vector_layer, name)

    @property
    def history(self):
        """Undo step names, oldest first, up to the current state (what Edit > Undo would undo, last)."""
        hist = self._history()
        head, cur, steps = _u64(hist), _u64(hist + 0x20), []
        n = _u64(head)  # first node
        while n != cur and n != head and len(steps) < 100000:
            steps.append(_pws_read(_u64(n + 0x10) + 0x10))  # PWDoCommand label at +0x10
            n = _u64(n)
        return steps

    def undo(self, steps=1):
        for _ in range(steps):
            self._run("Edit/Undo")

    def redo(self, steps=1):
        for _ in range(steps):
            self._run("Edit/Redo")

    def __eq__(self, other):
        return isinstance(other, Document) and other.ptr == self.ptr

    def __hash__(self):
        return hash(self.ptr)

    def __repr__(self):
        return f"<Document @ {self.ptr:#x}>"


def documents():
    """Open canvases, in tab order."""
    return [Document(ptr) for ptr, _ in mem.sp_vector(_app() + 0x300) if ptr]


def active_document():
    """The canvas in front (what the menus act on)."""
    ptr = _u64(_app() + 0x318)
    if not ptr or mem.rtti_name(ptr) != _DOC_CLASS:
        raise CspError("no open canvas")
    return Document(ptr)


class Image:
    """RGBA pixels, rows from the top: the pixel at (x, y) is data[(y * width + x) * 4:][:4] = r, g, b, a."""

    def __init__(self, width, height, data):
        self.width, self.height, self.data = width, height, bytes(data)

    def pixel(self, x, y):
        if not (0 <= x < self.width and 0 <= y < self.height):
            raise IndexError(f"({x}, {y}) is outside {self.width} x {self.height}")
        i = (y * self.width + x) * 4
        return tuple(self.data[i:i + 4])

    def save(self, path):
        """Write a PNG."""
        stride = self.width * 4
        rows = b"".join(b"\0" + self.data[y * stride:(y + 1) * stride] for y in range(self.height))

        def chunk(kind, body):
            return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

        with open(path, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", self.width, self.height, 8, 6, 0, 0, 0))
                    + chunk(b"IDAT", zlib.compress(rows, 6)) + chunk(b"IEND", b""))

    def __repr__(self):
        return f"<Image {self.width} x {self.height}>"


def _read_tga(path):
    """CSP's .tga export: color (BGR / BGRA) or, for gray content, gray (G / GA); raw or run-length encoded."""
    with open(path, "rb") as f:
        t = f.read()
    kind, (w, h), bpp, desc = t[2], struct.unpack_from("<HH", t, 12), t[16], t[17]
    gray = kind in (3, 11)
    if kind not in (2, 3, 10, 11) or (gray and bpp not in (8, 16)) or (not gray and bpp not in (24, 32)):
        raise CspError(f"unexpected Targa export (type {kind}, {bpp} bits)")
    px, n, i = bpp // 8, w * h, 18 + t[0]
    if kind in (2, 3):
        src = t[i:i + n * px]
    else:
        out = bytearray()
        while len(out) < n * px:
            head = t[i]
            k = (head & 0x7F) + 1
            if head & 0x80:  # one pixel repeated
                out += t[i + 1:i + 1 + px] * k
                i += 1 + px
            else:  # k literal pixels
                out += t[i + 1:i + 1 + px * k]
                i += 1 + px * k
        src = out
    if len(src) != n * px:
        raise CspError("truncated Targa export")
    rgba = bytearray(b"\xff") * (n * 4)
    if gray:
        for c in range(3):
            rgba[c::4] = src[0::px]
        if px == 2:
            rgba[3::4] = src[1::2]
    else:
        rgba[0::4], rgba[1::4], rgba[2::4] = src[2::px], src[1::px], src[0::px]
        if px == 4:
            rgba[3::4] = src[3::4]
    if not desc & 0x20:  # rows stored bottom-up
        stride = w * 4
        rgba = b"".join(rgba[y * stride:(y + 1) * stride] for y in range(h - 1, -1, -1))
    return Image(w, h, rgba)


# --- export: File > Export (Single Layer) with two detours that answer its dialogs while csp.export runs
_EXPORT_MENU = {".png": ".png (PNG)", ".jpg": ".jpg (JPEG)", ".jpeg": ".jpg (JPEG)", ".bmp": ".bmp (BMP)",
                ".tif": ".tif (TIFF)", ".tiff": ".tif (TIFF)", ".tga": ".tga (Targa)",
                ".psd": ".psd(Photoshop Document)", ".psb": ".psb(Photoshop Big Document)"}
_pws_assign = _fn(0x1417FBF30, None, c_void_p, c_void_p)
_FILE_DIALOG_T = ctypes.CFUNCTYPE(c_uint32, *[c_void_p] * 7)  # 1412201e0(PWString* out path, int*, dir, name, ...)
_EXPORT_SETTINGS_T = ctypes.CFUNCTYPE(c_int32, *[c_void_p] * 5)  # 140686f10(sp<Doc>, settings, int* preview, path, sp)


def _export_hooks():
    """Install the detours once per CSP run; they live on the menu module so reloading csp doesn't re-patch."""
    import menu  # owns _detour
    hooks = getattr(menu, "_csp_export", None)
    if hooks is not None:
        return hooks
    hooks = {"path": None}

    def on_file_dialog(out, *rest):
        try:
            if hooks["path"]:
                s = _pws_new(hooks["path"])
                try:
                    _pws_assign(out, ctypes.addressof(s))  # the chosen file
                finally:
                    _pws_free(ctypes.addressof(s))
                return 1  # OK
        except BaseException:  # never unwind into CSP: show the real dialog instead
            pass
        return file_dialog(out, *rest)

    def on_settings(doc, settings, preview, path, extra):
        if hooks["path"]:
            c_int32.from_address(preview).value = 0  # no "Export preview" dialog
            _sp_release(doc)  # the original consumes both shared_ptrs
            _sp_release(extra)
            return 1  # OK: keep the last-used settings 1406898d0 loaded
        return settings_dialog(doc, settings, preview, path, extra)

    file_dialog = menu._detour(0x1412201E0, "48895c2408 48896c2410 4889742418 57", _FILE_DIALOG_T, on_file_dialog)[0]
    settings_dialog = menu._detour(0x140686F10, "4055 53 56 57 4154 4155 4156 4157 488dac2418fdffff",
                                   _EXPORT_SETTINGS_T, on_settings)[0]
    menu._csp_export = hooks
    return hooks


_IAT_OPEN = 0x1431E4218  # GetOpenFileNameW import slot; its only caller 1413320e0 is CSP's Open-file dialog
_OPEN_T = ctypes.WINFUNCTYPE(c_int32, c_void_p)  # BOOL GetOpenFileNameW(OPENFILENAMEW*)


def _open_hooks():
    """Swap the GetOpenFileNameW import once per CSP run: while hooks["path"] is set, the next Open dialog
    returns that file instead of showing."""
    import menu  # owns _keep / _write_code; holding the hook there survives reloading csp
    hooks = getattr(menu, "_csp_open", None)
    if hooks is not None:
        return hooks
    hooks = {"path": None}
    real = _OPEN_T(_u64(_va(_IAT_OPEN)))

    def on_open(ofn):  # OPENFILENAMEW: +0x30 lpstrFile, +0x38 nMaxFile, +0x64 nFileOffset, +0x66 nFileExtension
        try:
            path, hooks["path"] = hooks["path"], None  # answer one dialog only
            raw = (path or "").encode("utf-16-le") + b"\0" * 4  # double NUL ends a multi-select list too
            if path and len(raw) // 2 <= c_uint32.from_address(ofn + 0x38).value:
                ctypes.memmove(_u64(ofn + 0x30), raw, len(raw))
                ctypes.c_uint16.from_address(ofn + 0x64).value = len(path) - len(os.path.basename(path))
                ext = os.path.splitext(path)[1]
                ctypes.c_uint16.from_address(ofn + 0x66).value = len(path) - len(ext) + 1 if ext else 0
                return 1
        except BaseException:  # never unwind into CSP: show the real dialog instead
            pass
        return real(ofn)

    cb = _OPEN_T(on_open)
    menu._keep.append(cb)
    menu._write_code(_va(_IAT_OPEN), ctypes.cast(cb, c_void_p).value.to_bytes(8, "little"))
    menu._csp_open = hooks
    return hooks


def _run_with_file(command, path):
    """Run a menu command whose Open dialog picks `path`."""
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        raise CspError(f"no file {path!r}")
    hooks = _open_hooks()
    hooks["path"] = path
    try:
        run(command)
    finally:
        hooks["path"] = None


def open_document(path):
    """File > Open `path` (.clip, .psd, .png, ...) without the dialog. Returns its Document, now active
    (the existing one if it was already open)."""
    _app()
    before = set(documents())
    _run_with_file("File/Open", path)
    new = [d for d in documents() if d not in before]
    return new[0] if new else active_document()


# --- CSP's message box 141207270(PWString* text, title, buttons, icon, default) -> 1 OK, 2 Cancel, 3 Yes / Save,
# 4 No / Don't save (| 0x100 when "don't show again" is ticked). buttons 5 and 6 = Save / Don't save / Cancel.
_MESSAGE_BOX = 0x141207270
_MESSAGE_T = ctypes.CFUNCTYPE(c_uint32, c_void_p, c_void_p, c_int32, c_int32, c_int32)
_DONT_SAVE = 4


def _message_hooks():
    """Detour the message box once per CSP run: while hooks["answer"] = {buttons: result}, a box with those
    buttons returns that result without showing."""
    import menu
    hooks = getattr(menu, "_csp_message", None)
    if hooks is not None:
        return hooks
    hooks = {"answer": None}

    def on_box(text, title, buttons, icon, default):
        try:
            result = (hooks["answer"] or {}).get(buttons)
            if result:
                return result
        except BaseException:  # never unwind into CSP: show the box instead
            pass
        return box(text, title, buttons, icon, default)

    box = menu._detour(_MESSAGE_BOX, "48895c2420 55 56 57 4154 4155 4156 4157", _MESSAGE_T, on_box)[0]
    menu._csp_message = hooks
    return hooks


# --- transform mode: PWResizableImageInfo (+0x10 kind, +0x28 vector of corner points {double x, y},
# +0x40/+0x48 scale, +0x50 rotation, +0x58/+0x60 center). 141b10b50 mirrors it for Edit > Transform > Flip
# Horizontal; its caller 142348a90 snapshots it for undo, redraws the old and new area and records the step.
_FLIP_INFO = 0x141B10B50
_INFO_T = ctypes.CFUNCTYPE(None, c_void_p)


def _edit_info(info, edit):
    """Apply edit = (point_map, sx, sy) to a PWResizableImageInfo: center and corners go through
    point_map((x, y)) -> (x, y) and the scale is multiplied by sx, sy. -> done."""
    # shortcut: raster only (kind 0/1); vector kinds also keep a copy of the points at +0x98.
    if c_uint32.from_address(info + 0x10).value >= 2:
        return False
    point_map, sx, sy = edit
    first, end = _u64(info + 0x28), _u64(info + 0x30)
    for p in [info + 0x58, *range(first, end, 16)]:  # center, then corners
        xy = (c_double * 2).from_address(p)
        xy[:] = point_map(tuple(xy))
    for off, k in ((0x40, sx), (0x48, sy)):
        c_double.from_address(info + off).value *= k
    return True


def _transform_hooks():
    """Detour the flip once per CSP run: while hooks["edit"] is set, the next flip runs _edit_info instead
    (looked up at call time, so reloading csp takes effect)."""
    import menu
    hooks = getattr(menu, "_csp_transform", None)
    if hooks is not None:
        return hooks
    hooks = {"edit": None, "done": False}

    def on_flip(info):
        try:
            edit, hooks["edit"] = hooks["edit"], None
            if edit:
                hooks["done"] = sys.modules["csp"]._edit_info(info, edit)
                return  # not done: left as it was, _transform() cancels
        except BaseException:  # never unwind into CSP
            return
        flip(info)

    flip = menu._detour(_FLIP_INFO, "48895c2418 57 4883ec20 488b5128", _INFO_T, on_flip)[0]
    menu._csp_transform = hooks
    return hooks


def _call_virtual(obj, offset):
    ctypes.CFUNCTYPE(None, c_void_p)(_u64(_u64(obj) + offset))(obj)


# --- selection (PWSelectArea: +8 owner of the mask raster, +0x18 int[4] bounds ltrb, +0x28 state)
_select_area = _fn(0x14046F810, c_void_p, c_void_p, c_void_p)  # (sp<PWSelectArea>* out, sp<Doc>)
_select_cmd = _fn(0x1422B77B0, c_void_p, c_void_p, c_void_p, c_int32)  # (sp<cmd>* out, sp<area> by value, 0)
_area_reset = _fn(0x141AA5EF0, None, c_void_p, c_int32, c_int32, c_int32)  # (area, w, h, clear)
_get_raster = _fn(0x141246E60, c_void_p, c_void_p, c_void_p)  # (owner, sp<PWVOffscreen>* out)
_value_init = _fn(0x14181BDB0, None, c_void_p)
_value_set = _fn(0x14181C460, None, c_void_p, c_int32, c_int32)
_fill_rect = _fn(0x1426609F0, c_int32, c_void_p, c_void_p, c_void_p, c_int32)  # (raster, int[4] ltrb, value, 0)
_push = _fn(0x141374A40, None, c_void_p, c_void_p)  # (executor, sp<cmd> by value): run + record
_selection_changed = _fn(0x140486EF0, None, c_void_p)  # (sp<Doc>): UI refresh after a selection change
_selection_changed2 = _fn(0x140486E40, None, c_void_p)
# "don't show this again" warning settings, by name (1404a83b0: the empty Selection-from-Layer warning)
_pref_get = _fn(0x141382720, c_int32, c_void_p)  # (PWString* name) -> show
_pref_set = _fn(0x141382860, None, c_void_p, c_int32)  # (name, show)


# --- PWString: +0 vtable, +8 heap wchar* (0 = inline at +0x20), +0x10 shared ctrl, +0x18 len, +0x1C cap, size 0x30
_pws_init = _fn(0x1417FBEA0, None, c_void_p)
_pws_resize = _fn(0x1417FCD70, c_void_p, c_void_p, c_int32)
_pws_free = _fn(0x1417FBED0, None, c_void_p)


def _pws_read(s):
    n = int.from_bytes(mem.read(s + 0x18, 4), "little", signed=True)
    buf = _u64(s + 8) or s + 0x20
    return mem.read(buf, 2 * n).decode("utf-16-le") if n > 0 else ""


def _pws_new(text):
    s = ctypes.create_string_buffer(0x30)
    _pws_init(ctypes.addressof(s))
    raw = text.encode("utf-16-le")
    _pws_resize(ctypes.addressof(s), len(raw) // 2)  # also writes the terminating NUL
    ctypes.memmove(c_uint64.from_buffer(s, 8).value or ctypes.addressof(s) + 0x20, raw, len(raw))
    return s


# --- layers
_new_raster_layer = _fn(0x1404778B0, None, c_void_p)
_new_folder = _fn(0x140476940, None, c_void_p)
_new_vector_layer = _fn(0x1404AB9C0, None, c_void_p)  # same shape as the raster one, no dialog
_current_layer = _fn(0x14046DBD0, c_void_p, c_void_p, c_void_p)
_set_layer_name = _fn(0x141932CC0, None, c_void_p, c_void_p)
_LAYER_CLASS = ".?AVPWLayer@Planeswalker@@"  # raster layers and folders alike
# Layer-palette actions (undoable, repaint), each taking sp<Doc> by value. "Selected" ones act on the selection.
_select_layer = _fn(0x140496B60, None, c_void_p, c_void_p, c_int32, c_int32)  # (doc, sp<Layer>, record, 0)
_set_visible = _fn(0x14045A3C0, None, c_void_p, c_int32, c_int32, c_void_p)  # (doc, set (0=toggle), on, sp<Layer>*)
_NULL_SP = (c_uint64 * 2)()  # "no layer": _set_visible then toggles the selected layers
_set_opacity = _fn(0x140458520, None, c_void_p, c_int32, c_int32)  # selected: (doc, 0..256, filter; 1 skips ours)
_set_blend_mode = _fn(0x140458390, None, c_void_p, c_int32)  # selected: (doc, mode)
_set_locked = _fn(0x1404562B0, None, c_void_p, c_int32, c_int32)  # selected: (doc, set (0=toggle), on)
_set_lock_alpha = _fn(0x140452BC0, None, c_void_p, c_int32, c_int32)  # same shape
_set_clip = _fn(0x140452D90, None, c_void_p, c_int32, c_int32)  # same shape
# text layers: layer attachment 200 is a PWTextLayerManager holding one or more text blocks
_TEXT = 200
_get_attachment = _fn(0x141A23F60, c_int32, c_void_p, c_int32, c_void_p)  # (layer, key, sp* out) -> found
_text_count = _fn(0x1402312D0, c_int32, c_void_p)  # (mgr) -> blocks
_text_data = _fn(0x141AEC7A0, c_void_p, c_void_p, c_void_p, c_int32)  # (mgr, sp<PWData>* out, block): UTF-8


class BlendMode(enum.IntEnum):
    """The Layer palette's blending modes (checked live against the palette's label for each number)."""
    NORMAL, DARKEN, MULTIPLY, COLOR_BURN, LINEAR_BURN, SUBTRACT, DARKER_COLOR = range(7)
    LIGHTEN, SCREEN, COLOR_DODGE, GLOW_DODGE, ADD, ADD_GLOW, LIGHTER_COLOR = range(7, 14)
    OVERLAY, SOFT_LIGHT, HARD_LIGHT, VIVID_LIGHT, LINEAR_LIGHT, PIN_LIGHT, HARD_MIX = range(14, 21)
    DIFFERENCE, EXCLUSION, HUE, SATURATION, COLOR, BRIGHTNESS = range(21, 27)
    THROUGH = 30  # folders only
    DIVIDE = 36
    # 27-29 and 31-35 are CSP-internal (Erase, Dissolve, Background, Replace alpha, ...), 37+ show as "Unknown"


class Layer:
    """A layer or folder of `document`. Setters go through CSP's Layer-palette actions (undoable like the UI;
    visibility isn't undoable in CSP either), selecting the layer for the moment they run."""

    # shortcut: raw pointer, no ref. The class check catches most deleted layers, but freed memory can still
    # carry the vtable; hold a shared_ptr once there's a need to keep layers across deletes.
    def __init__(self, ptr, document):
        self.ptr, self.document = ptr, document

    def _live(self):
        if mem.rtti_name(self.ptr) != _LAYER_CLASS:
            raise CspError("layer no longer exists")
        return self.ptr

    def _int(self, off):
        return c_int32.from_address(self._live() + off).value

    def _sp(self):
        ctrl = _u64(self._live() + 0x58)  # enable_shared_from_this: weak_ptr to itself at +0x50
        if not ctrl or c_int32.from_address(ctrl + 8).value <= 0:
            raise CspError("layer no longer exists")
        _addref(ctrl)
        return (c_uint64 * 2)(self.ptr, ctrl)

    def _on_selection(self, action, *args):
        """Run an action that applies to the selected layers with only this one selected, then reselect.
        A multi-selection the user had comes back as just its current layer."""
        doc = self.document
        before = doc.active_layer
        doc.active_layer = self
        try:
            sp = doc._sp()
            action(ctypes.addressof(sp), *args)
        finally:
            if before != self:
                doc.active_layer = before

    def _toggle(self, now, on, action, *args):
        # The actions' "set to value" modes don't reliably apply (visible, clip, lock alpha); their toggle does.
        if bool(on) != now:
            self._on_selection(action, *args)

    @property
    def visible(self):
        return self._int(0x170) != 0

    @visible.setter
    def visible(self, on):
        self._toggle(self.visible, on, _set_visible, 0, 0, ctypes.addressof(_NULL_SP))

    @property
    def opacity(self):
        """0..100, as the Layer palette shows it (CSP stores 0..256)."""
        return round(self._int(0x1A0) * 100 / 256)

    @opacity.setter
    def opacity(self, percent):
        if not 0 <= percent <= 100:
            raise CspError(f"opacity must be 0..100, not {percent!r}")
        self._on_selection(_set_opacity, round(percent * 256 / 100), 0)

    @property
    def blend_mode(self):
        """A BlendMode (a plain int for modes the palette doesn't offer)."""
        mode = self._int(0x1A4)
        return BlendMode(mode) if mode in BlendMode._value2member_map_ else mode

    @blend_mode.setter
    def blend_mode(self, mode):
        if mode not in BlendMode._value2member_map_:
            raise CspError(f"unknown blend mode {mode!r}; see csp.BlendMode")
        self._on_selection(_set_blend_mode, int(mode))

    @property
    def locked(self):
        return bool(self._int(0x174) & 1)

    @locked.setter
    def locked(self, on):
        self._toggle(self.locked, on, _set_locked, 0, 0)

    @property
    def lock_transparency(self):
        return bool(self._int(0x174) & 0x10)

    @lock_transparency.setter
    def lock_transparency(self, on):
        self._toggle(self.lock_transparency, on, _set_lock_alpha, 0, 0)

    @property
    def clip(self):
        """Clip to layer below."""
        return self._int(0x178) != 0

    @clip.setter
    def clip(self, on):
        self._toggle(self.clip, on, _set_clip, 0, 0)

    @property
    def is_text(self):
        mgr = (c_uint64 * 2)()
        found = _get_attachment(self._live(), _TEXT, ctypes.addressof(mgr))
        _sp_release(ctypes.addressof(mgr))
        return bool(found)

    @property
    def text(self):
        """A text layer's text (several text blocks are joined with newlines), or None for other layers.
        Read-only: CSP doesn't redraw text written from outside the text tool."""
        mgr = (c_uint64 * 2)()
        if not _get_attachment(self._live(), _TEXT, ctypes.addressof(mgr)):
            return None
        try:
            blocks = []
            for i in range(_text_count(mgr[0])):
                data = (c_uint64 * 2)()
                _text_data(mgr[0], ctypes.addressof(data), i)  # PWData: +8 bytes, +0x18 length
                try:
                    n = c_int32.from_address(data[0] + 0x18).value if data[0] else 0
                    blocks.append(mem.read(_u64(data[0] + 8), n).decode("utf-8") if n > 0 else "")
                finally:
                    _sp_release(ctypes.addressof(data))
            return "\n".join(blocks)
        finally:
            _sp_release(ctypes.addressof(mgr))

    def image(self):
        """This layer alone as an Image: the canvas exported with every other layer hidden for the moment
        (with its opacity and blend mode; a clipped layer shows only where its base is, so it comes out empty).
        Visibility isn't undoable in CSP, so this adds no undo steps; everything is shown again afterwards."""
        ancestors, up = [], self.parent
        while up:
            ancestors.append(up)
            up = up.parent
        changed = []

        def show_only(layers):
            for layer in layers:
                want = layer == self or layer in ancestors
                if layer.visible != want:
                    layer.visible = want
                    changed.append(layer)
                if layer in ancestors:
                    show_only(layer.children)

        try:
            show_only(self.document.layers)
            return self.document.image()
        finally:
            for layer in reversed(changed):
                layer.visible = not layer.visible

    @property
    def is_folder(self):
        return bool(self._int(0x1BC) & 1)

    @property
    def parent(self):
        """The folder this layer is in, or None at the top level."""
        up = _u64(self._live() + 0x1C0)
        return Layer(up, self.document) if up and _u64(up + 0x1C0) else None

    @property
    def children(self):
        """Layers in this folder, top to bottom like the Layer palette ([] for non-folders)."""
        if not self.is_folder:
            return []
        return [Layer(ptr, self.document) for ptr, _ in reversed(mem.sp_vector(self.ptr + 0x1E0)) if ptr]

    @property
    def name(self):
        return _pws_read(self._live() + 0x130)

    @name.setter
    def name(self, text):
        # Not undoable and the layer palette may only repaint on the next change.
        s = _pws_new(text)
        try:
            _set_layer_name(self._live(), ctypes.addressof(s))
        finally:
            _pws_free(ctypes.addressof(s))

    def _run(self, command):
        """Select this layer (only it) and run a Layer-menu command on it. Returns the active layer afterwards."""
        self.document.active_layer = self
        return self.document._run(command)

    def duplicate(self):
        """Layer > Duplicate Layer. Returns the copy (selected, above this one)."""
        return self._run("Layer/Duplicate Layer")

    def remove(self):
        """Layer > Delete Layer. The layer below (or above) becomes selected."""
        self._run("Layer/Delete Layer")

    def merge_down(self):
        """Layer > Merge with layer below. Returns the merged layer."""
        return self._run("Layer/Merge with layer below")

    @property
    def bounds(self):
        """(left, top, right, bottom) around this layer's pixels (right/bottom exclusive), None if empty.
        Only the part on the canvas counts. Uses Layer > Selection from Layer and undoes it again, so the
        selection is left as it was."""
        doc = self.document
        steps = len(doc.history)
        doc.active_layer = self
        if not enabled("Layer/Selection from Layer/Create Selection"):  # e.g. a folder
            return None
        key = _pws_new("canvasmodelselectareanotcreatedwarning")  # the empty-layer warning's "show" setting
        try:
            show = _pref_get(key)
            _pref_set(key, 0)
            try:
                doc._run("Layer/Selection from Layer/Create Selection")
            finally:
                _pref_set(key, show)
        finally:
            _pws_free(ctypes.addressof(key))
        try:
            return doc._selection_bounds()
        finally:
            doc.undo(len(doc.history) - steps)

    def _transform(self, point_map, sx=1.0, sy=1.0):
        """Edit > Transform with the transform changed by _transform_hooks, then OK (one undo step)."""
        doc, hooks = self.document, _transform_hooks()
        if self.bounds is None:  # CSP would show "There is no object to transform"
            raise CspError(f"{self!r} is empty")
        self._run("Edit/Transform/Scale/Rotate")
        hooks.update(edit=(point_map, sx, sy), done=False)
        try:
            run("Edit/Transform/Flip Horizontal")  # the detour turns this flip into our change
        finally:
            done, hooks["edit"] = hooks["done"], None
        if not done:
            doc._run("Edit/Transform/Cancel")
            raise CspError(f"can't transform {self!r} (not a raster layer?)")
        doc._run("Edit/Transform/OK")

    def translate(self, dx, dy):
        """Move this layer's pixels by (dx, dy) canvas pixels (one undo step "Scale/Rotate", like dragging it
        with Edit > Transform). Raster layers only."""
        self._transform(lambda p: (p[0] + dx, p[1] + dy))

    def resize(self, sx, sy=None):
        """Scale this layer's pixels by sx (and sy, default sx; 0.5 = half size) around the center of its
        (on-canvas) bounds, like Edit > Transform > Scale (one undo step). Raster layers only."""
        sy = sx if sy is None else sy
        if sx <= 0 or sy <= 0:
            raise CspError(f"scale must be > 0, not {(sx, sy)!r}")
        left, top, right, bottom = self.bounds or (0, 0, 0, 0)
        cx, cy = (left + right) / 2, (top + bottom) / 2
        self._transform(lambda p: (cx + (p[0] - cx) * sx, cy + (p[1] - cy) * sy), sx, sy)

    def flip(self, direction="horizontal"):
        """Mirror this layer within its own bounds: "horizontal" or "vertical" (one undo step)."""
        self._run("Edit/Transform/" + _flip_name(direction))
        self.document._run("Edit/Transform/OK")

    _ORDER = {"top": "Top", "up": "Up", "down": "Down", "bottom": "Bottom"}

    def move(self, where):
        """Layer > Layer Order: "top", "up", "down" or "bottom" (within its folder, as CSP does)."""
        if where not in self._ORDER:
            raise CspError(f"move() takes one of {list(self._ORDER)}, not {where!r}")
        self._run("Layer/Layer Order/" + self._ORDER[where])

    def __eq__(self, other):
        return isinstance(other, Layer) and other.ptr == self.ptr

    def __hash__(self):
        return hash(self.ptr)

    def __repr__(self):
        try:
            return f"<Layer {self.name!r} @ {self.ptr:#x}>"
        except CspError:
            return f"<Layer (deleted) @ {self.ptr:#x}>"


def _flip_name(direction):
    if direction not in ("horizontal", "vertical"):
        raise CspError(f'flip() takes "horizontal" or "vertical", not {direction!r}')
    return "Flip " + direction.capitalize()


def current_layer():
    return active_document().active_layer


def new_raster_layer(name=None):
    return active_document().new_raster_layer(name)


def new_folder(name=None):
    return active_document().new_folder(name)


def undo_group(label="Script"):
    """`with csp.undo_group("My script"): ...` makes the block one undo step on the active canvas."""
    return active_document().undo_group(label)


# --- menu commands: what a click does is PWView::ExecuteCommand(view, command, param). Only menu views route
# commands to the active canvas, so we send them through the main menu bar.
_execute = _fn(0x1411A8100, c_uint32, c_void_p, c_int32, c_int32)
_item_command = _fn(0x141235310, c_int32, c_void_p)  # resolves PWMenuItem's command key (+0x48)
_intern = _fn(0x1411ABE20, c_int32, c_void_p)
_ev_init = _fn(0x141209E90, c_void_p, c_void_p, c_int32)
_ev_set_command = _fn(0x140571B30, None, c_void_p, c_int32)
_ev_set_param = _fn(0x1406C1060, None, c_void_p, c_int32)
_ev_free = _fn(0x141209F80, None, c_void_p)
_msg_init = _fn(0x14119A4D0, c_void_p, c_void_p)
_msg_set_int = _fn(0x1408A6070, None, c_void_p, c_int32, c_int32)
_msg_free = _fn(0x14119A500, None, c_void_p)
_ev_to_msg = _fn(0x14120A0C0, None, c_void_p, c_void_p)
_msg_to_ev = _fn(0x14120A210, None, c_void_p, c_void_p)
_send = _fn(0x1411A8380, c_uint32, c_void_p, c_void_p)
_ev_enabled = _fn(0x1406AF740, c_int32, c_void_p)
_EV_QUERY, _KEY_ROUTE = _va(0x143D702F8), _va(0x143D6EFD0)


def _bar():
    window = _u64(_app() + 0x478)  # main PWWindow -> +0x288 PWScrollView -> +0x290 PWMenuBar
    scroll = _u64(window + 0x288) if window else None
    bar = _u64(scroll + 0x290) if scroll else None
    if not bar or mem.rtti_name(bar) != ".?AVPWMenuBar@Planeswalker@@":
        raise CspError("main menu bar not found")
    return bar


def _query(view, cmd, param):
    """Ask the views whether cmd is handled / enabled, as ExecuteCommand does first. -> (handled, enabled)."""
    ev, msg = ctypes.create_string_buffer(0x100), ctypes.create_string_buffer(0x100)  # CSP uses 0x70 / 0x30
    _ev_init(ev, _intern(_EV_QUERY))
    _ev_set_command(ev, cmd)
    _ev_set_param(ev, param)
    _msg_init(msg)
    try:
        _ev_to_msg(ev, msg)
        _msg_set_int(msg, _intern(_KEY_ROUTE), 1)
        if not _send(view, msg):
            return False, False
        _msg_to_ev(ev, msg)
        return True, bool(_ev_enabled(ev))
    finally:
        _msg_free(msg)
        _ev_free(ev)


def commands():
    """{"Layer/New Raster Layer": (command, param), ...} for every item in the main menu."""
    out = {}

    def walk(menu, path):
        for it, _ in mem.sp_vector(menu + 8):
            title = _pws_read(it + 0x10)
            p = f"{path}/{title}" if title else path  # the root's groups are untitled
            sub = _u64(it + 0xB0)
            if sub:
                walk(sub, p)
            elif title:
                cmd = _item_command(it)
                if cmd:
                    out.setdefault(p.lstrip("/"), (cmd, c_int32.from_address(it + 0x68).value))

    main = _u64(_app() + 0x98)  # sp<PWMainMenu>
    if main:
        walk(_u64(main + 8), "")
    return out


def _resolve(command):
    if isinstance(command, int):
        return command, 0
    found = {k.casefold(): v for k, v in commands().items()}.get(command.casefold())
    if not found:
        raise CspError(f"no menu command {command!r}; see csp.commands()")
    return found


def enabled(command):
    """Whether a menu command (path or id) can run right now, as its menu item would show."""
    return _query(_bar(), *_resolve(command))[1]


def run(command):
    """Run a main-menu command like clicking it: a path from commands() (case-insensitive) or a command id.
    Commands that open a dialog return once it's closed."""
    cmd, param = _resolve(command)
    bar = _bar()
    handled, on = _query(bar, cmd, param)
    if not handled:
        raise CspError(f"nothing handles command {command!r}")
    if not on:
        raise CspError(f"{command!r} is disabled right now")
    _execute(bar, cmd, param)


# --- drawing colors: PWCurrentColorManager (1414854b0 returns it). +8 active slot (0 main, 1 sub), +0x10 + slot*0x58
# a PWColorSaver whose RGB is at +0x10 as three uint32 channels holding the 8-bit value in every byte.
_color_manager = _fn(0x1414854B0, c_void_p)
_color_copy = _fn(0x1413954C0, c_void_p, c_void_p, c_void_p)  # (out, src): copy-construct
_color_free = _fn(0x141395710, None, c_void_p)
_color_set_rgb = _fn(0x141396200, None, c_void_p, c_void_p)  # (color, uint32[3])
_color_set_slot = _fn(0x141486BA0, None, c_uint32, c_void_p)  # (slot, color*): sets it, makes it active, notifies


def _get_color(slot):
    _app()  # UI-thread check
    rgb = _color_manager() + 0x10 + slot * 0x58 + 0x10
    return tuple(c_uint32.from_address(rgb + 4 * i).value >> 24 for i in range(3))


def _set_color(slot, rgb):
    if len(rgb) != 3 or not all(isinstance(v, int) and 0 <= v <= 255 for v in rgb):
        raise CspError(f"color must be three ints 0..255, not {rgb!r}")
    _app()
    mgr = _color_manager()
    active = c_int32.from_address(mgr + 8).value
    _write_slot(mgr, slot, rgb)
    if active in (0, 1) and active != slot:
        _write_slot(mgr, active, None)  # writing a slot also selects it: reselect the one that was active


def _write_slot(mgr, slot, rgb):
    color = ctypes.create_string_buffer(0x80)  # PWColorSaver is 0x58
    _color_copy(color, mgr + 0x10 + slot * 0x58)
    try:
        if rgb:
            _color_set_rgb(color, (c_uint32 * 3)(*[v * 0x01010101 for v in rgb]))
        _color_set_slot(slot, color)
    finally:
        _color_free(color)


def main_color():
    """The main drawing color as (r, g, b), 0..255."""
    return _get_color(0)


def sub_color():
    return _get_color(1)


def set_main_color(rgb):
    _set_color(0, rgb)


def set_sub_color(rgb):
    """Set the sub color; whichever of main/sub was active stays active."""
    _set_color(1, rgb)
