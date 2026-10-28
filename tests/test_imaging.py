import numpy as np
from PIL import Image

from app import imaging
from make_test_photo import make


def test_refine_fills_damage_hole_but_keeps_handle_opening():
    a = np.zeros((400, 400), np.uint8)
    a[150:380, 60:340] = 255                  # tas-body
    a[40:150, 120:140] = 255                  # hengsel links
    a[40:150, 260:280] = 255                  # hengsel rechts
    a[40:60, 120:280] = 255                   # hengsel boven -> opening binnen hengsel
    a[250:254, 150:250] = 0                   # "kras" die het model als achtergrond zag
    a[10:14, 10:14] = 255                     # los snippertje achtergrond
    out, rep = imaging._refine(a.copy())
    assert (out[250:254, 150:250] == 255).all(), "beschadiging moet behouden blijven"
    assert (out[70:140, 150:250] == 0).all(), "ruimte binnen hengsel moet open blijven"
    assert out[10:14, 10:14].max() == 0 and rep["islands_removed"] == 1
    assert rep["holes_filled"] == 1


def test_compose_keeps_every_product_pixel_identical():
    rng = np.random.default_rng(0)
    orig = Image.fromarray(rng.integers(0, 255, (300, 400, 3), dtype=np.uint8))
    mask = np.zeros((300, 400), np.uint8); mask[50:250, 100:300] = 255; mask[50, 100:300] = 120
    for style in ("studio", "wit"):
        comp = imaging.compose(orig, Image.fromarray(mask), style)
        assert comp.integrity["passed"] and comp.integrity["mismatched_pixels"] == 0
        ox, oy = comp.offset
        src = np.asarray(orig)[51:250, 100:300]
        dst = np.asarray(comp.image)[oy + 1:oy + 200, ox:ox + 200]
        assert np.array_equal(src, dst)


def test_scene_scales_background_not_product():
    rng = np.random.default_rng(1)
    orig = Image.fromarray(rng.integers(0, 255, (300, 400, 3), dtype=np.uint8))
    mask = np.zeros((300, 400), np.uint8); mask[50:250, 100:300] = 255
    scene = Image.new("RGB", (2000, 3000), (200, 180, 160))
    comp = imaging.compose(orig, Image.fromarray(mask), "scene", scene, (0.5, 0.4))
    assert comp.integrity["passed"] and comp.integrity["scale"] == 1.0


def test_save_lossless_roundtrip(tmp_path):
    img = Image.fromarray(np.random.default_rng(2).integers(0, 255, (120, 160, 3), dtype=np.uint8))
    p = imaging.save_lossless(img, tmp_path / "x")
    assert np.array_equal(np.asarray(Image.open(p).convert("RGB")), np.asarray(img))


def test_full_pipeline_keeps_handle_and_damage(tmp_path):
    src = tmp_path / "tas.jpg"
    make(str(src))
    res = imaging.process_photo(src, tmp_path, "studio")
    integ = res["integrity"]
    assert integ["passed"] and integ["mismatched_pixels"] == 0
    mask = np.asarray(Image.open(res["mask_path"])) > 128
    assert mask[300:520, 620:980].sum() > 15000, "hengsel ontbreekt in de uitsnede"
    assert mask[760:900, 600:735].mean() > 0.5, "kras-gebied ontbreekt"


def test_scene_place_true_scale_and_exact_pixels():
    from app import scenes
    rng = np.random.default_rng(3)
    orig = Image.fromarray(rng.integers(0, 255, (300, 400, 3), dtype=np.uint8))
    mask = np.zeros((300, 400), np.uint8); mask[60:240, 80:320] = 255   # tas 240 px breed
    scene = Image.new("RGB", (1536, 1024), (200, 190, 170))
    anchors = {"tafel_links_x": 0.1, "tafel_rechts_x": 0.7, "plaats_x": 0.4, "plaats_y": 0.75}
    out, integ = scenes.place(orig, Image.fromarray(mask), scene, scenes.PRESETS["parijs"], anchors,
                              {"breedte": 30.0})
    assert integ["passed"] and integ["mismatched_pixels"] == 0 and integ["scale"] == 1.0
    # tafel 60 cm = 0.6*1536 px; tas 30 cm moet half zo breed zijn als de tafel na schalen
    table_px_scaled = 0.6 * 1536 * integ["scene_scale"]
    assert abs(240 / table_px_scaled - 0.5) < 0.01
    assert not integ["dims_assumed"]


def test_blank_photo_is_rejected(tmp_path):
    import pytest
    src = tmp_path / "leeg.png"
    Image.new("RGB", (1200, 1200), (240, 240, 240)).save(src)
    with pytest.raises(ValueError, match="Geen tas herkend"):
        imaging.process_photo(src, tmp_path, "studio")
