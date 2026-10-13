import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import lens_usage_stats as lus  # noqa: E402


def row(model="NIKON Z 7", fl=100, scale=1.0, w=8256, h=5504, **kw):
    r = {"Model": model, "FocalLength": fl, "ScaleFactor35efl": scale,
         "ImageWidth": w, "ImageHeight": h, "LensModel": "Test 70-200mm"}
    r.update(kw)
    return r


# ---------------------------------------------------------------- EFL math

def test_uncropped_photo_keeps_its_focal_length():
    r = lus.compute_effective([row()])[0]
    assert r["Zoom"] == 1.0 and r["EFL35"] == 100


def test_half_width_crop_doubles_the_focal_length():
    r = lus.compute_effective([row(w=4128, h=2752)])[0]
    assert r["Zoom"] == 2.0 and r["EFL35"] == 200


def test_portrait_orientation_is_the_same_frame():
    r = lus.compute_effective([row(w=5504, h=8256)])[0]
    assert r["Zoom"] == 1.0


def test_square_crop_is_an_aspect_change_not_a_zoom():
    # 3:2 -> 1:1 keeping the full height: the short side is untouched
    r = lus.compute_effective([row(w=5504, h=5504)])[0]
    assert r["Zoom"] == 1.0 and r["AspectChanged"] is True


def test_crop_sensor_scale_is_applied():
    r = lus.compute_effective([row(model="NIKON D200", fl=50, scale=1.5,
                                   w=3872, h=2592)])[0]
    assert r["EFL35"] == 75


def test_scale_falls_back_to_the_35mm_focal_length():
    r = lus.compute_effective([row(model="NIKON D200", fl=50, scale=None,
                                   FocalLengthIn35mmFormat=75,
                                   w=3872, h=2592)])[0]
    assert r["EFL35"] == 75


def test_resized_export_is_left_out_of_the_effective_table():
    r = lus.compute_effective([row(w=2048, h=1365)])[0]
    assert r["Zoom"] > lus.MAX_PLAUSIBLE_ZOOM and r["EFL35"] is None


def test_unknown_model_uses_the_largest_export_as_full_frame():
    rows = lus.compute_effective([
        row(model="Phone X", fl=6, scale=4.0, w=4000, h=3000),
        row(model="Phone X", fl=6, scale=4.0, w=2000, h=1500),
    ])
    assert rows[0]["NativeFrom"] == "largest export"
    assert rows[0]["EFL35"] == 24 and rows[1]["EFL35"] == 48


def test_in_camera_crop_mode_is_not_counted_twice():
    # Z 7 in DX mode: 1.5x scale, a smaller full frame (5408x3600)
    rows = lus.compute_effective([
        row(fl=50, scale=1.0),
        row(fl=50, scale=1.5, w=5504, h=3669),
    ])
    assert rows[1]["Zoom"] == 1.0 and rows[1]["EFL35"] == 75


def test_zoom_compact_scale_wobble_stays_one_frame():
    # whole-number 35 mm focal lengths: 25/4.1 = 6.10 vs 300/50 = 6.0
    rows = lus.compute_effective([
        row(model="DMC-TZ10", fl=4.1, scale=6.0976, w=4000, h=3000),
        row(model="DMC-TZ10", fl=50, scale=6.0, w=2000, h=1500),
    ])
    assert rows[1]["Zoom"] == 2.0


# ---------------------------------------------------------------- helpers

def test_blank_lens_names_fall_back():
    assert lus.lens_name({"LensModel": "-- mm f/--", "LensID": "X 50mm"}) \
        == "X 50mm"
    assert lus.lens_name({"Model": "C5050Z"}) == "(no lens info) C5050Z"


def test_manual_lens_uses_lens_info_or_says_manual():
    assert lus.lens_name({"LensModel": "-- mm f/--",
                          "LensInfo": "28mm f/1.4"}) == "28mm f/1.4 (manual)"
    assert lus.lens_name({"LensModel": "-- mm f/--", "Model": "NIKON D700"}) \
        == "(manual lens) NIKON D700"


def test_dedupe_keeps_the_best_subfolder():
    a = row(DateTimeOriginal="2024:01:01 10:00:00", Subfolder="sRGB",
            SourceFile="a.jpg")
    b = row(DateTimeOriginal="2024:01:01 10:00:00", Subfolder="TIFF16",
            SourceFile="b.tif")
    c = row(SourceFile="no_date.jpg")
    out = lus.dedupe([a, b, c], ["TIFF16", "sRGB"])
    assert sorted(r["SourceFile"] for r in out) == ["b.tif", "no_date.jpg"]


def test_dedupe_fills_tags_the_best_copy_lacks():
    tiff = row(DateTimeOriginal="2024:01:01 10:00:00", Subfolder="TIFF16",
               LensModel=None, w=8256, h=5504)
    jpeg = row(DateTimeOriginal="2024:01:01 10:00:00", Subfolder="sRGB",
               LensModel="NIKKOR 85mm", w=2048, h=1365)
    out = lus.dedupe([jpeg, tiff], ["TIFF16", "sRGB"])
    assert len(out) == 1
    assert out[0]["LensModel"] == "NIKKOR 85mm"
    assert out[0]["ImageWidth"] == 8256      # size stays the chosen file's


def test_event_is_the_root_when_the_root_is_an_event(tmp_path):
    f = tmp_path / "240101_Event" / "_EXPORT" / "sRGB" / "a.jpg"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"x")
    found = lus.find_photos([tmp_path / "240101_Event"], "export",
                            "_EXPORT", ["sRGB"])
    assert found[0][2] == "240101_Event"


def test_find_photos_export_mode(tmp_path):
    for p in ["2024/240101_Event/01_Z7/_EXPORT/sRGB/a.jpg",
              "2024/240101_Event/01_Z7/_EXPORT/WEB/a.jpg",
              "2024/240101_Event/01_Z7/a.NEF",
              "2024/240101_Event/01_Z7/raw.jpg"]:
        f = tmp_path / p
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"x")
    found = lus.find_photos([tmp_path / "2024"], "export", "_EXPORT",
                            ["TIFF16", "sRGB"])
    assert [(p.name, e, s) for p, _, e, s in found] == \
        [("a.jpg", "240101_Event", "sRGB")]
    every = lus.find_photos([tmp_path / "2024"], "all", "_EXPORT", ["sRGB"])
    assert len(every) == 3


def test_aperture_bins():
    assert lus._aperture_bin(1.8) == "f/1.4"
    assert lus._aperture_bin(2.8) == "f/2.8"
    assert lus._aperture_bin(3.5) == "f/2.8"
    assert lus._aperture_bin(6.3) == "f/5.6"


def test_csv_round_trip(tmp_path):
    rows = lus.compute_effective([row(w=4128, h=2752, SourceFile="x.jpg")])
    path = tmp_path / "out.csv"
    lus.write_csv(rows, path)
    again = lus.compute_effective(lus.read_csv(path))
    assert again[0]["EFL35"] == 200


# ---------------------------------------------------------------- real exiftool

@pytest.mark.skipif(shutil.which("exiftool") is None,
                    reason="exiftool not on the PATH")
def test_real_exiftool_end_to_end(tmp_path, capsys):
    Image = pytest.importorskip("PIL.Image")
    # tags are written under an ASCII name (exiftool's Windows command line
    # is not Unicode), then the event folder gets its Japanese name
    sub = tmp_path / "2025" / "ascii" / "_EXPORT"
    for name, size, folder in [("full.jpg", (600, 400), "TIFF16"),
                               ("crop.jpg", (300, 200), "TIFF16"),
                               ("full_s.jpg", (600, 400), "sRGB")]:
        (sub / folder).mkdir(parents=True, exist_ok=True)
        Image.new("RGB", size).save(sub / folder / name)
    common = ["-Make=TestCam", "-Model=Test Camera", "-FocalLength=50",
              "-FocalLengthIn35mmFormat=75", "-FNumber=2.8",
              "-LensModel=Test 50mm f/1.8", "-overwrite_original"]
    subprocess.run(["exiftool", *common, "-DateTimeOriginal=2025:03:01 10:00:00",
                    str(sub / "TIFF16" / "full.jpg"),
                    str(sub / "sRGB" / "full_s.jpg")], check=True,
                   capture_output=True)
    subprocess.run(["exiftool", *common, "-DateTimeOriginal=2025:03:01 10:00:05",
                    str(sub / "TIFF16" / "crop.jpg")], check=True,
                   capture_output=True)
    (tmp_path / "2025" / "ascii").rename(tmp_path / "2025" / "250301_東京_Test")
    out_csv = tmp_path / "out.csv"
    assert lus.main(["--roots", str(tmp_path / "2025"), "--csv", str(out_csv),
                     "--lens", "50mm", "--by-folder"]) == 0
    text = capsys.readouterr().out
    assert "1 duplicate exports of the same shot counted once" in text
    rows = {Path(r["SourceFile"]).name: r for r in lus.read_csv(out_csv)}
    assert set(rows) == {"full.jpg", "crop.jpg"}
    effective = {n: r for n, r in
                 ((r["SourceFile"], r) for r in
                  lus.compute_effective(list(rows.values())))}
    efl = sorted(r["EFL35"] for r in effective.values())
    assert efl == [75, 150]
    assert "250301_東京_Test" in text
