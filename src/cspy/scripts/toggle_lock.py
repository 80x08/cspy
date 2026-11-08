# Locks the selected layer if it's unlocked, unlocks it otherwise.
layer = csp.current_layer()
layer.locked = not layer.locked
print(f"{layer.name!r} is now {'locked' if layer.locked else 'unlocked'}.")
