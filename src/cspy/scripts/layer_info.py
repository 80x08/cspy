# Shows the layer tree of the front canvas with each layer's settings. Changes nothing.


def line(layer, depth):
    flags = [f for f, on in (("hidden", not layer.visible), ("locked", layer.locked),
                             ("alpha-locked", layer.lock_transparency), ("clipped", layer.clip)) if on]
    mode = layer.blend_mode
    blend = mode.name.replace("_", " ").title() if isinstance(mode, csp.BlendMode) else f"blend {mode}"
    kind = "[folder] " if layer.is_folder else "[text] " if layer.is_text else ""
    mark = "> " if layer == current else "  "
    print(f"{mark}{'    ' * depth}{kind}{layer.name}  {layer.opacity}%  {blend}  {' '.join(flags)}")
    for child in layer.children:
        line(child, depth + 1)


doc = csp.active_document()
current = doc.active_layer
for layer in doc.layers:
    line(layer, 0)
