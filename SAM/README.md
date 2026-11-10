# Segment Anything 3 for cspy

A "SAM selection" window for CLIP STUDIO PAINT: pick an object on the current layer by clicks, a box or text
("hair"), preview it, and press Apply: it becomes the canvas selection (one undo step, through the cspy console).
Runs Meta's SAM 3 on CUDA when PyTorch has it, else on the CPU.

| File | What |
|---|---|
| `../src/cspy/scripts/sam_select.py` | the starter, inside CSP (Scripts menu): exports the current layer, opens the window |
| `sam_gui.py` | the window (tkinter, runs in `.venv`; CSP stays usable) |
| `sam_models.py` | the SAM 3 wrapper: clicks / box (one object) and text / example boxes (every match) |

## Use
In CSP select the layer (optionally draw a rectangle selection around the object: it becomes the starting box)
and run Scripts > sam_select (or `python console.py sam_select`). The starter finds this folder via the
`CSPY_SAM` environment variable, next to the repo's `src`, or at `D:\cspy\SAM`.

In the window:
- **Object** mode: left click adds, right click removes, drag a box. One object.
- **Concept** mode: type what to select ("hair", "jacket") and press Enter; every match is selected. Drag boxes as
  examples (left) or counter-examples (right). The confidence slider filters weak matches.
- Grow / shrink and Invert are applied in CSP (`expand_selection` / `shrink_selection` / `invert_selection`).
- **Apply to CSP** sets the selection on the front canvas (it must still be the same size).

## Setup (once, from this folder)
```powershell
uv venv .venv --python 3.12
# CPU PyTorch; on an NVIDIA machine use the CUDA build (https://pytorch.org) and SAM 3 runs on the GPU
uv pip install --python .venv\Scripts\python.exe torch torchvision --index-url https://download.pytorch.org/whl/cpu
# weights: models\sam3\sam3.pt (gated facebook/sam3 on Hugging Face, or a mirror such as 1038lab/sam3)
git clone https://huggingface.co/1038lab/sam3 models\sam3
# Meta's code; apply the CPU patches below, then install it
git clone https://github.com/facebookresearch/sam3 src\sam3
uv pip install --python .venv\Scripts\python.exe .\src\sam3
# imports sam3 needs that its package doesn't declare
uv pip install --python .venv\Scripts\python.exe "setuptools<81" einops scipy opencv-python-headless pycocotools pillow
```

CPU patches in `src\sam3` (needed without CUDA, harmless with it; marked `# cspy` in the code; reinstall from
`src\sam3` after changing them):
- `sam3/model/sam3_tracker_utils.py`: import `edt_triton` inside the function that uses it (triton is CUDA-only
  and only used for training clicks).
- `sam3/model/decoder.py`, `sam3/model/position_encoding.py`: caches built on `"cuda"` unconditionally.
- `sam3/perflib/fused.py`: `addmm_act` is a fused bfloat16 CUDA kernel; plain float32 linear + activation off CUDA.
- `sam3/model/*.py`: `pin_memory()` only with CUDA.

## Notes
- On the CPU (Ryzen AI Max): model load ~9 s, image encode ~8.5 s (once per window), click ~0.05 s, text
  prompt ~1 s.
- Without CUDA SAM 3 prints "Skipping the post-processing step" (its hole filling uses triton); the masks are fine.
- File > Import > Image does not place a canvas-sized PNG 1:1, so masks go through `doc.select_mask`.
- `.venv`, `models` and `src` are not in git (see `.gitignore`).
