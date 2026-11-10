# For every .clip / .psd / .png / .jpg in Pictures\cspy\in: opens it, scales it to fit 512 x 512, exports a PNG
# to Pictures\cspy\thumbs and closes it without saving. Your files are not changed. No dialogs.
# Exercises open_document, resize_image, export and close(discard=True).
import os

SIZE = 512
base = os.path.join(os.path.expanduser("~"), "Pictures", "cspy")
src, out = os.path.join(base, "in"), os.path.join(base, "thumbs")
os.makedirs(src, exist_ok=True)
os.makedirs(out, exist_ok=True)

names = [n for n in sorted(os.listdir(src)) if n.lower().endswith((".clip", ".psd", ".png", ".jpg", ".jpeg"))]
if not names:
    print(f"Put some .clip / .psd / .png / .jpg files into\n{src}\nand run this again.")
done = []
for name in names:
    doc = csp.open_document(os.path.join(src, name))
    try:
        scale = min(SIZE / doc.width, SIZE / doc.height, 1)
        if scale < 1:
            doc.resize_image(max(1, round(doc.width * scale)), max(1, round(doc.height * scale)))
        doc.export(os.path.join(out, os.path.splitext(name)[0] + ".png"))
        done.append(f"{name}: {doc.width} x {doc.height}")
    finally:
        doc.close(discard=True)
if done:
    print(f"Wrote {len(done)} thumbnails to {out}:\n" + "\n".join(done))
