"""Segment Anything 3 selection window for CLIP STUDIO PAINT. Started by the sam_select script (inside CSP);
sends the result back through the cspy console as the canvas selection.

    .venv\\Scripts\\pythonw.exe sam_gui.py layer.png [--box L T R B] [--port 50555]

Object mode: left click = add, right click = remove, left drag = box (one object).
Concept mode: type what to select ("hair"); left / right drag = example / counter-example boxes;
every match is selected.
"""
import argparse
import os
import queue
import socket
import tempfile
import threading
import time
import tkinter as tk
from tkinter import ttk

import numpy as np
from PIL import Image, ImageTk

import sam_models

VIEW = 900  # longest side of the preview, screen pixels


class App:
    def __init__(self, root, args):
        self.root, self.args = root, args
        self.image = load_rgb(args.image)
        self.w, self.h = self.image.size
        self.k = min(VIEW / self.w, VIEW / self.h, 1.0)  # image -> screen
        self.view = self.image.resize((round(self.w * self.k), round(self.h * self.k)), Image.BILINEAR)
        self.points, self.labels, self.boxes = [], [], []  # boxes: [((l, t, r, b), positive)]
        self.box = tuple(args.box) if args.box else None  # object mode box
        self.mask = None
        self.model = None
        self.jobs, self.results = queue.Queue(), queue.Queue()
        self.drag = None
        self._build()
        threading.Thread(target=self._worker, daemon=True).start()
        self.jobs.put(("load", None))
        root.after(50, self._poll)

    # --- window
    def _build(self):
        r = self.root
        r.title("SAM selection")
        self.canvas = tk.Canvas(r, width=self.view.width, height=self.view.height, cursor="crosshair",
                                highlightthickness=0, bg="#222")
        self.canvas.grid(row=0, column=0, rowspan=20, padx=6, pady=6)
        self.canvas.bind("<ButtonPress-1>", lambda e: self._press(e, True))
        self.canvas.bind("<ButtonPress-3>", lambda e: self._press(e, False))
        self.canvas.bind("<B1-Motion>", self._motion)
        self.canvas.bind("<B3-Motion>", self._motion)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.canvas.bind("<ButtonRelease-3>", self._release)

        side = ttk.Frame(r, padding=6)
        side.grid(row=0, column=1, sticky="n")
        self.status = tk.StringVar(value="Loading model...")
        ttk.Label(side, textvariable=self.status, wraplength=260, foreground="#06c").pack(anchor="w", pady=(0, 8))

        self.mode = tk.StringVar(value="object")
        ttk.Label(side, text="Mode").pack(anchor="w")
        ttk.Radiobutton(side, text="Object: clicks / one box", variable=self.mode, value="object",
                        command=self._changed).pack(anchor="w")
        ttk.Radiobutton(side, text="Concept: text, every match", variable=self.mode, value="concept",
                        command=self._changed).pack(anchor="w")

        ttk.Label(side, text="Text (concept mode)").pack(anchor="w", pady=(8, 0))
        self.text = tk.StringVar()
        entry = ttk.Entry(side, textvariable=self.text, width=30)
        entry.pack(anchor="w")
        entry.bind("<Return>", lambda e: (self.mode.set("concept"), self._changed()))
        ttk.Label(side, text="Confidence (concept mode)").pack(anchor="w", pady=(8, 0))
        self.threshold = tk.DoubleVar(value=0.5)
        ttk.Scale(side, from_=0.05, to=0.95, variable=self.threshold, length=220,
                  command=lambda v: self._changed(delay=400)).pack(anchor="w")

        ttk.Separator(side).pack(fill="x", pady=8)
        ttk.Label(side, text="Grow (+) / shrink (-) px").pack(anchor="w")
        self.grow = tk.IntVar(value=0)
        ttk.Spinbox(side, from_=-200, to=200, textvariable=self.grow, width=8).pack(anchor="w")
        self.invert = tk.BooleanVar()
        ttk.Checkbutton(side, text="Invert", variable=self.invert, command=self._draw).pack(anchor="w", pady=4)
        ttk.Label(side, text="Preview opacity").pack(anchor="w")
        self.opacity = tk.DoubleVar(value=0.5)
        ttk.Scale(side, from_=0.1, to=0.9, variable=self.opacity, length=220,
                  command=lambda v: self._draw()).pack(anchor="w")

        ttk.Separator(side).pack(fill="x", pady=8)
        row = ttk.Frame(side)
        row.pack(anchor="w")
        ttk.Button(row, text="Undo prompt", command=self._undo).pack(side="left")
        ttk.Button(row, text="Clear", command=self._clear).pack(side="left", padx=4)
        row = ttk.Frame(side)
        row.pack(anchor="w", pady=(8, 0))
        ttk.Button(row, text="Apply to CSP", command=lambda: self._apply(False)).pack(side="left")
        ttk.Button(row, text="Apply && close", command=lambda: self._apply(True)).pack(side="left", padx=4)
        ttk.Label(side, text="\nLeft click: add   Right click: remove\nDrag: box (concept: right drag = exclude)\n"
                             "Enter in the text box: run concept", foreground="#666").pack(anchor="w")
        self._draw()

    # --- prompts
    def _xy(self, e):
        return (min(max(e.x / self.k, 0), self.w - 1), min(max(e.y / self.k, 0), self.h - 1))

    def _press(self, e, left):
        self.drag = (self._xy(e), left, None)

    def _motion(self, e):
        if not self.drag:
            return
        (x0, y0), left, item = self.drag
        if item:
            self.canvas.delete(item)
        item = self.canvas.create_rectangle(x0 * self.k, y0 * self.k, e.x, e.y, outline="#0f0" if left else "#f33",
                                            dash=(4, 2))
        self.drag = ((x0, y0), left, item)

    def _release(self, e):
        if not self.drag:
            return
        (x0, y0), left, _ = self.drag
        self.drag = None
        x1, y1 = self._xy(e)
        if abs(x1 - x0) * self.k > 5 and abs(y1 - y0) * self.k > 5:
            box = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
            if self.mode.get() == "concept":
                self.boxes.append((box, left))
            else:
                self.box = box
        elif self.mode.get() == "object":
            self.points.append((x0, y0))
            self.labels.append(1 if left else 0)
        self._changed()

    def _undo(self):
        if self.mode.get() == "concept" and self.boxes:
            self.boxes.pop()
        elif self.points:
            self.points.pop()
            self.labels.pop()
        else:
            self.box = None
        self._changed()

    def _clear(self):
        self.points, self.labels, self.boxes, self.box, self.mask = [], [], [], None, None
        self.text.set("")
        self._draw()

    def _changed(self, delay=0):
        if self.model is None:
            return
        if self.mode.get() == "concept":
            job = ("concept", (self.text.get().strip(), list(self.boxes), self.threshold.get()))
            if not job[1][0] and not job[1][1]:
                self.mask = None
                return self._draw()
        else:
            if not self.points and not self.box:
                self.mask = None
                return self._draw()
            job = ("object", (list(self.points), list(self.labels), self.box))
        self.pending = job
        self.root.after(delay, self._submit)
        self._draw()

    def _submit(self):
        job, self.pending = getattr(self, "pending", None), None
        if job:
            while not self.jobs.empty():  # only the latest prompt matters
                self.jobs.get_nowait()
            self.jobs.put(job)

    # --- model thread
    def _worker(self):
        while True:
            kind, arg = self.jobs.get()
            t0 = time.perf_counter()
            try:
                if kind == "load":
                    self.results.put(("status", "Loading model (SAM 3 on the CPU takes a while)..."))
                    model = sam_models.Sam3()
                    self.results.put(("status", f"{model.name}: encoding the image..."))
                    model.set_image(self.image)
                    self.results.put(("ready", (model, time.perf_counter() - t0)))
                elif kind == "object":
                    mask, score = self.model.object(*arg)
                    self.results.put(("mask", (mask, f"score {score:.2f}", time.perf_counter() - t0)))
                elif kind == "concept":
                    mask, n = self.model.concept(*arg)
                    self.results.put(("mask", (mask, f"{n} match{'es' if n != 1 else ''}", time.perf_counter() - t0)))
            except Exception as e:  # shown in the window, the thread keeps serving
                self.results.put(("status", f"Error: {type(e).__name__}: {e}"))

    def _poll(self):
        while not self.results.empty():
            kind, arg = self.results.get_nowait()
            if kind == "status":
                self.status.set(arg)
            elif kind == "ready":
                self.model, seconds = arg
                self.status.set(f"{self.model.name} ready ({seconds:.0f} s). Click or drag on the image.")
                self._changed()
            elif kind == "mask":
                self.mask, info, seconds = arg
                self.status.set(f"{self.model.name}: {info}, {self.mask.sum()} px ({seconds:.1f} s)")
                self._draw()
        self.root.after(50, self._poll)

    # --- drawing
    def _shown_mask(self):
        if self.mask is None:
            return None
        return ~self.mask if self.invert.get() else self.mask

    def _draw(self):
        img = self.view
        mask = self._shown_mask()
        if mask is not None:
            small = Image.fromarray(mask.astype(np.uint8) * 255).resize(img.size, Image.NEAREST)
            red = Image.new("RGB", img.size, (255, 40, 40))
            img = Image.composite(Image.blend(img, red, self.opacity.get()), img, small)
        self.photo = ImageTk.PhotoImage(img)
        c, k = self.canvas, self.k
        c.delete("all")
        c.create_image(0, 0, image=self.photo, anchor="nw")
        if self.mode.get() == "object" and self.box:
            c.create_rectangle(*(v * k for v in self.box), outline="#0f0", width=2)
        if self.mode.get() == "concept":
            for box, positive in self.boxes:
                c.create_rectangle(*(v * k for v in box), outline="#0f0" if positive else "#f33", width=2)
        for (x, y), label in zip(self.points, self.labels):
            c.create_oval(x * k - 5, y * k - 5, x * k + 5, y * k + 5, fill="#0f0" if label else "#f33",
                          outline="white")

    # --- back to CSP
    def _apply(self, close):
        mask = self.mask
        if mask is None or not mask.any():
            self.status.set("Nothing to apply yet.")
            return
        rgba = np.zeros(mask.shape + (4,), np.uint8)
        rgba[mask] = 255
        path = os.path.join(self.args.tmp, "selection.tga")
        Image.fromarray(rgba, "RGBA").save(path)
        grow = self.grow.get()
        code = (f"doc = csp.active_document()\n"
                f"if (doc.width, doc.height) != ({self.w}, {self.h}):\n"
                f"    raise csp.CspError('the front canvas is not {self.w} x {self.h} any more')\n"
                f"with doc.undo_group('SAM selection'):\n"
                f"    doc.select_mask(csp._read_tga({path!r}))\n"
                + (f"    doc.expand_selection({grow})\n" if grow > 0 else "")
                + (f"    doc.shrink_selection({-grow})\n" if grow < 0 else "")
                + ("    doc.invert_selection()\n" if self.invert.get() else "")
                + "print('ok')\n")
        try:
            reply = send(code, self.args.port)
        except OSError as e:
            reply = f"can't reach CSP's cspy console: {e}"
        if reply.strip().endswith("ok"):
            self.status.set("Applied to CSP (Edit > Undo there to take it back).")
            if close:
                self.root.destroy()
        else:
            self.status.set("CSP: " + reply.strip()[-400:])


def load_rgb(path):
    """The layer as RGB; transparent pixels composited onto white."""
    image = Image.open(path).convert("RGBA")
    return Image.alpha_composite(Image.new("RGBA", image.size, (255, 255, 255, 255)), image).convert("RGB")


def send(code, port):
    """cspy console protocol (console.py): code + NUL in, output + NUL back."""
    with socket.create_connection(("127.0.0.1", port), timeout=60) as s:
        s.sendall(code.encode() + b"\0")
        data = b""
        while not data.endswith(b"\0"):
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
    return data.rstrip(b"\0").decode(errors="replace")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("image")
    p.add_argument("--box", nargs=4, type=float)
    p.add_argument("--port", type=int, default=50555)
    p.add_argument("--cleanup", action="store_true", help="delete the image's folder on exit (sam_select.py)")
    args = p.parse_args()
    args.tmp = tempfile.mkdtemp(prefix="cspy-sam-gui-")
    root = tk.Tk()
    App(root, args)
    try:
        root.mainloop()
    finally:
        import shutil
        shutil.rmtree(args.tmp, ignore_errors=True)
        if args.cleanup:
            shutil.rmtree(os.path.dirname(os.path.abspath(args.image)), ignore_errors=True)


if __name__ == "__main__":
    main()
