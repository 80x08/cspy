"""SAM 3 backend for sam_gui.py:

    model = Sam3()                # CUDA if PyTorch has it, else the CPU
    model.set_image(pil_rgb)      # slow (the image encoder), once per image
    model.object(points, labels, box)               -> (bool mask, score)   one object from clicks / a box
    model.concept(text, boxes, threshold)           -> (bool mask, count)   every match of a text / example boxes

points: [(x, y)], labels: [1 add / 0 remove], box: (l, t, r, b) or None, boxes: [((l, t, r, b), positive)].
All coordinates are image pixels.
"""
import os

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
SAM3_CHECKPOINT = os.path.join(HERE, "models", "sam3", "sam3.pt")


class Sam3:
    """Meta's SAM 3 (facebookresearch/sam3) on CUDA when PyTorch has it, else the CPU."""

    def __init__(self, checkpoint=SAM3_CHECKPOINT):
        import torch
        from sam3 import build_sam3_image_model
        from sam3.model.sam3_image_processor import Sam3Processor
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.name = f"SAM 3 ({self.device})"
        self.model = build_sam3_image_model(device=self.device, checkpoint_path=checkpoint, load_from_HF=False,
                                            enable_inst_interactivity=True)
        self.processor = Sam3Processor(self.model, device=self.device)

    def set_image(self, image):
        self.size = image.size
        self.state = self.processor.set_image(image)

    def object(self, points, labels, box):
        kw = {}
        if points:
            kw.update(point_coords=np.array(points, np.float32), point_labels=np.array(labels, np.int32))
        if box:
            kw["box"] = np.array(box, np.float32)
        masks, scores, _ = self.model.predict_inst(self.state, multimask_output=True, **kw)
        best = int(np.argmax(scores))
        return np.asarray(masks[best]) > 0, float(scores[best])

    def concept(self, text, boxes, threshold):
        p, state = self.processor, self.state
        p.reset_all_prompts(state)
        p.confidence_threshold = threshold
        if text:
            state = p.set_text_prompt(text, state)
        w, h = self.size
        for (l, t, r, b), positive in boxes:  # SAM 3 wants normalized center x, center y, width, height
            state = p.add_geometric_prompt([(l + r) / 2 / w, (t + b) / 2 / h, (r - l) / w, (b - t) / h],
                                           bool(positive), state)
        self.state = state
        masks = state.get("masks")
        if masks is None or len(masks) == 0:
            return np.zeros((h, w), bool), 0
        return masks.any(dim=0).squeeze(0).cpu().numpy(), len(masks)

