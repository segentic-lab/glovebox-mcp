"""ScreenParser detector option: class mapping, the OCR merge (pure, no GPU) and the selection rules."""
import os

import pytest

import vision


def box(x1, y1, x2, y2):
    return [x1, y1, x2, y2]


def test_every_mapped_class_has_a_type_and_containers_are_unmapped():
    assert vision.SP_TYPES["Button"] == "button" and vision.SP_TYPES["Search Field"] == "input"
    for container in ("Window", "Screen", "Side Bar", "Toolbar", "Navigation Bar", "Table", "List", "Menu"):
        assert container not in vision.SP_TYPES


def test_button_absorbs_its_label_and_text_lines_survive():
    dets = [{"bbox": box(0.10, 0.10, 0.30, 0.15), "cls": "Button", "conf": 0.9},
            {"bbox": box(0.00, 0.00, 1.00, 1.00), "cls": "Window", "conf": 0.9},     # container: dropped
            {"bbox": box(0.10, 0.30, 0.90, 0.60), "cls": "Text", "conf": 0.5}]       # paragraph with OCR inside: dropped
    ocr = [{"bbox": box(0.12, 0.11, 0.28, 0.14), "content": "Save"},
           {"bbox": box(0.10, 0.31, 0.80, 0.34), "content": "First line"},
           {"bbox": box(0.10, 0.36, 0.70, 0.39), "content": "Second line"}]
    out = vision.merge_detections(dets, ocr)
    kinds = [(e["type"], e["content"], e["interactive"], e["class"]) for e in out]
    assert ("button", "Save", True, "Button") in kinds
    assert ("text", "First line", False, "OCR") in kinds and ("text", "Second line", False, "OCR") in kinds
    assert len(out) == 3                                           # Window and the Text block are gone
    assert all(e["content"] is not None for e in out)              # nothing left to caption


def test_icon_without_text_is_captioned_last_and_images_are_not_interactive():
    dets = [{"bbox": box(0.5, 0.5, 0.55, 0.55), "cls": "Utility Button", "conf": 0.4},
            {"bbox": box(0.0, 0.0, 0.2, 0.2), "cls": "Image", "conf": 0.6}]
    ocr = [{"bbox": box(0.7, 0.7, 0.9, 0.72), "content": "Footer"}]
    out = vision.merge_detections(dets, ocr)
    assert [e["content"] for e in out][0] == "Footer"              # OCR text first ...
    assert [e["content"] for e in out][1:] == [None, None]         # ... caption-needing boxes last
    img = next(e for e in out if e["class"] == "Image")
    assert img["type"] == "image" and img["interactive"] is False
    btn = next(e for e in out if e["class"] == "Utility Button")
    assert btn["type"] == "button" and btn["interactive"] is True


def test_overlapping_detections_keep_the_smaller_and_icon_inside_text_is_dropped():
    dets = [{"bbox": box(0.1, 0.1, 0.5, 0.5), "cls": "Button", "conf": 0.3},
            {"bbox": box(0.1, 0.1, 0.45, 0.45), "cls": "Button", "conf": 0.8},    # 81% of the bigger one: keep this
            {"bbox": box(0.60, 0.60, 0.62, 0.62), "cls": "App Icon", "conf": 0.5}]  # inside an OCR line: dropped
    ocr = [{"bbox": box(0.55, 0.59, 0.95, 0.63), "content": "a line of text"}]
    out = vision.merge_detections(dets, ocr)
    buttons = [e for e in out if e["type"] == "button"]
    assert len(buttons) == 1 and buttons[0]["bbox"][2] == 0.45
    assert not [e for e in out if e["class"] == "App Icon"]
    assert [e for e in out if e["class"] == "OCR"][0]["content"] == "a line of text"


def test_text_block_without_ocr_is_kept_for_captioning():
    out = vision.merge_detections([{"bbox": box(0.1, 0.1, 0.4, 0.2), "cls": "Heading", "conf": 0.5}], [])
    assert out == [{"type": "text", "bbox": [0.1, 0.1, 0.4, 0.2], "content": None, "interactive": False,
                    "class": "Heading", "conf": 0.5}]


def test_detector_selection_rules(monkeypatch, tmp_path):
    w = str(tmp_path / "best.pt")
    monkeypatch.setattr(vision, "SCREENPARSER_WEIGHTS", w)
    monkeypatch.delenv("GLOVEBOX_DETECTOR", raising=False)
    assert vision.detector_info()["effective"] == "omniparser"              # auto, no weights
    monkeypatch.setenv("GLOVEBOX_DETECTOR", "screenparser")
    d = vision.detector_info()
    assert d["effective"] == "screenparser" and not d["screenparser_weights"] and "install-screenparser" in d["reason"]
    open(w, "wb").write(b"x")
    monkeypatch.setenv("GLOVEBOX_DETECTOR", "auto")
    assert vision.detector_info()["effective"] == "screenparser"            # auto, weights present
    monkeypatch.setenv("GLOVEBOX_DETECTOR", "omniparser")
    assert vision.detector_info()["effective"] == "omniparser"              # explicit override
    monkeypatch.setenv("GLOVEBOX_DETECTOR", "nonsense")
    assert vision.detector_info()["requested"] == "auto"


def test_status_exposes_detector():
    info = vision.omniparser_info()
    assert set(info["detector"]) == {"requested", "effective", "screenparser_weights", "reason"}


def test_container_keeps_a_child_of_another_class_and_ties_go_to_confidence():
    dets = [{"bbox": box(0.1, 0.1, 0.6, 0.15), "cls": "Text Input", "conf": 0.7},
            {"bbox": box(0.57, 0.11, 0.59, 0.14), "cls": "Utility Button", "conf": 0.4},   # clear (x) inside the input
            {"bbox": box(0.1, 0.5, 0.3, 0.55), "cls": "Button", "conf": 0.3},
            {"bbox": box(0.1, 0.5, 0.3, 0.55), "cls": "Link", "conf": 0.9}]               # identical box: Link wins
    out = vision.merge_detections(dets, [{"bbox": box(0.11, 0.11, 0.3, 0.14), "content": "Search"}])
    classes = sorted(e["class"] for e in out)
    assert classes == ["Link", "Text Input", "Utility Button"], classes
    assert next(e for e in out if e["class"] == "Text Input")["content"] == "Search"


def test_degenerate_and_out_of_image_boxes_are_dropped_or_clamped():
    dets = [{"bbox": box(0.5, 0.5, 0.5004, 0.6), "cls": "Badge", "conf": 0.9},     # 0.4 px wide at 1000 px
            {"bbox": box(0.9, 0.9, 1.3, 1.2), "cls": "Button", "conf": 0.9}]       # spills past the edge
    out = vision.merge_detections(dets, [], size=(1000, 1000))
    assert [e["class"] for e in out] == ["Button"]
    assert out[0]["bbox"] == [0.9, 0.9, 1.0, 1.0]
    assert vision._center(out[0]["bbox"], 1000, 1000) == [950, 950]
    assert vision._center([0.98, 0.98, 1.2, 1.2], 100, 100) == [99, 99]            # clamped into the display


def test_line_grouping_splits_menu_bar_gaps():
    # "File" "Edit" far apart on one tesseract line, "Sign" "in" adjacent on another
    data = {"text": ["File", "Edit", "Sign", "in"], "conf": [95, 95, 95, 95],
            "left": [10, 200, 10, 50], "top": [10, 10, 100, 100], "width": [30, 30, 35, 15], "height": [12, 12, 12, 12],
            "block_num": [1, 1, 2, 2], "par_num": [1, 1, 1, 1], "line_num": [1, 1, 1, 1]}
    elems, boxes = vision.group_tesseract(data, "line")
    assert [e["label"] for e in elems] == ["File", "Edit", "Sign in"]
    assert boxes[2] == (10, 100, 65, 112)
