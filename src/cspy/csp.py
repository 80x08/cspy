"""CLIP STUDIO PAINT 1.13.2 API, called in-process via ctypes. See RE_NOTES.md for each address.

    doc = csp.active_document()
    layer = doc.new_raster_layer("ink")      # undoable
    csp.run("Layer/Duplicate Layer")          # any main-menu command, like clicking it; see csp.commands()

Everything here must run on CSP's UI thread (console code and Scripts-menu scripts already do).
Raw memory helpers (read, va, rtti_name, ...) live in mem.py.
"""
import contextlib
import ctypes
import sys
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

    def select_all(self):
        self._run("Select/Select All")

    def deselect(self):
        self._run("Select/Deselect")

    def invert_selection(self):
        self._run("Select/Invert selected area")

    def new_raster_layer(self, name=None):
        """Same as Layer > New Raster Layer (undoable). Returns the new layer."""
        return self._create(_new_raster_layer, name)

    def new_folder(self, name=None):
        """Same as Layer > New Layer Folder (undoable). Returns the new folder."""
        return self._create(_new_folder, name)

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


class Layer:
    """A layer or folder of `document`. Setters go through CSP's Layer-palette actions (undoable like the UI;
    visibility isn't undoable in CSP either), selecting the layer for the moment they run. blend_mode is CSP's number: 0 Normal, 2 Multiply, 8 Screen, ...
    (the Layer palette's list order, as far as checked)."""

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
        return self._int(0x1A4)

    @blend_mode.setter
    def blend_mode(self, mode):
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
