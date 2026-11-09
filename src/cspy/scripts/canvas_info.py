# Shows the front canvas's size, colors, recent undo steps and text layers. Changes nothing.
doc = csp.active_document()
print(f"{doc.name}{' (unsaved changes)' if doc.modified else ''}")
print(f"{doc.width} x {doc.height} px at {doc.resolution:g} dpi")
print(f"main color {csp.main_color()}, sub color {csp.sub_color()}")
print("last undo steps:", ", ".join(doc.history[-5:]) or "none")
texts = [layer for layer in doc.walk() if layer.is_text]
for layer in texts:
    print(f"text layer {layer.name!r}: {layer.text!r}")
if not texts:
    print("no text layers")
