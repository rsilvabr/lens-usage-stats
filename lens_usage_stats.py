#!/usr/bin/env python3
"""
lens_usage_stats.py - which focal lengths, lenses and apertures you REALLY use.

Reads the EXIF of the photos you exported (the keepers, not the outtakes) with
exiftool and prints:

  - photos per camera and per lens
  - focal lengths per lens (the number the lens reported)
  - EFFECTIVE focal length: 35 mm equivalent AFTER your crop in editing.
    A 100 mm shot cropped to half the width framed like a 200 mm lens would.
  - an aperture x focal length grid for one lens (--lens)
  - optionally the same per event folder (--by-folder)
  - optionally every photo to a CSV (--csv), re-readable with --from-csv

Requires: Python 3.9+, exiftool on the PATH (https://exiftool.org).
No other dependency.

Usage:
  python lens_usage_stats.py --roots G:\\2024 G:\\2025
  python lens_usage_stats.py --roots G:\\2025 --lens 70-180 --csv exif.csv
  python lens_usage_stats.py --from-csv exif.csv --by-folder
"""
import argparse
import csv
import json
import os
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

# ============================================================================
# SETTINGS - edit these
# ============================================================================

# Folders to scan when --roots is not given ([] = the current folder).
ROOTS = []

# "export": only photos inside <EXPORT_MARKER>\<one of EXPORT_SUBFOLDERS>\
#           (the Capture One style "_EXPORT\sRGB" layout).
# "all":    every image under the roots.
FOLDER_MODE = "export"
EXPORT_MARKER = "_EXPORT"

# Export subfolders to read, best first. The same shot exported to several of
# them is counted once, from the first folder in this list (the largest file
# gives the most accurate crop). Leave resized folders (WEB, mobile) out: a
# downscaled export looks like a heavy crop.
EXPORT_SUBFOLDERS = ["TIFF16", "16B_JXL", "AdobeRGB", "sRGB"]

EXTENSIONS = {".jpg", ".jpeg", ".tif", ".tiff", ".jxl", ".png",
              ".heic", ".heif", ".dng"}

# Effective focal lengths are grouped in bins of this many mm.
EFL_BIN_MM = 5

# A crop zoom above this is treated as a resized export, not a crop: the
# photo still counts everywhere else, but not in the effective table.
MAX_PLAUSIBLE_ZOOM = 3.0

# Width and height cropped by ratios this far apart = the aspect ratio
# changed (e.g. 3:2 -> 1:1). The zoom is then the SMALLER ratio.
ASPECT_CHANGE_TOLERANCE = 0.10

# Native image size (long, short) of the FULL sensor, by model. Cameras not
# listed here use the largest export seen for that model instead, which is
# right as long as at least one photo was exported uncropped.
NATIVE_SIZES = {
    "NIKON Z 7": (8256, 5504), "NIKON Z 7_2": (8256, 5504),
    "NIKON Z 8": (8256, 5504), "NIKON Z 9": (8256, 5504),
    "NIKON D850": (8256, 5504),
    "NIKON Z 6": (6048, 4024), "NIKON Z 6_2": (6048, 4024),
    "NIKON Z 6_3": (6048, 4032),
    "NIKON Z f": (6048, 4032), "NIKON Z 5": (6016, 4016),
    "NIKON D780": (6048, 4024), "NIKON D750": (6016, 4016),
    "NIKON D610": (6016, 4016), "NIKON D600": (6016, 4016),
    "NIKON D810": (7360, 4912), "NIKON D800": (7360, 4912),
    "NIKON D800E": (7360, 4912),
    "NIKON D700": (4256, 2832), "NIKON D3": (4256, 2832),
    "NIKON D3S": (4256, 2832), "NIKON D3X": (6048, 4032),
    "NIKON D4": (4928, 3280), "NIKON D4S": (4928, 3280),
    "NIKON Df": (4928, 3280),
    "NIKON D5": (5568, 3712), "NIKON D6": (5568, 3712),
    "NIKON D500": (5568, 3712), "NIKON D7500": (5568, 3712),
    "NIKON D7200": (6000, 4000), "NIKON D7100": (6000, 4000),
    "NIKON D7000": (4928, 3264),
    "NIKON D300": (4288, 2848), "NIKON D300S": (4288, 2848),
    "NIKON D90": (4288, 2848), "NIKON D5000": (4288, 2848),
    "NIKON D200": (3872, 2592), "NIKON D80": (3872, 2592),
    "NIKON D70": (3008, 2000), "NIKON D70s": (3008, 2000),
    "NIKON D50": (3008, 2000), "NIKON D100": (3008, 2000),
    "FinePix S5Pro": (4256, 2848), "FinePix S3Pro": (4256, 2848),
}

EXIFTOOL = "exiftool"
EXIFTOOL_BATCH = 500

# ============================================================================

TAGS = ["Make", "Model", "SerialNumber", "LensModel", "LensID", "Lens",
        "LensInfo",
        "DateTimeOriginal", "SubSecTimeOriginal", "FocalLength",
        "FocalLengthIn35mmFormat", "ScaleFactor35efl", "FNumber",
        "ExposureTime", "ISO", "ImageWidth", "ImageHeight"]
NUMERIC = {"FocalLength", "FocalLengthIn35mmFormat", "ScaleFactor35efl",
           "FNumber", "ExposureTime", "ISO", "ImageWidth", "ImageHeight"}
BLANK_LENS = {"", "-- mm f/--", "unknown", "0 mm f/0"}

CSV_FIELDS = ["SourceFile", "Root", "Event", "Subfolder"] + TAGS
REPORT_FIELDS = ["LensName", "Scale", "Native", "NativeFrom", "Zoom",
                 "AspectChanged", "EFL35"]


# ---------------------------------------------------------------- discovery

def _is_image(name):
    return Path(name).suffix.lower() in EXTENSIONS


def find_photos(roots, mode, marker, subfolders):
    """Return [(path, root, event, subfolder)]. event = first folder below
    the root; subfolder = the export subfolder name ('' in mode 'all')."""
    wanted = {s.lower(): i for i, s in enumerate(subfolders)}
    found = []
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            print(f"[warning] root not found: {root}", file=sys.stderr)
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort()
            here = Path(dirpath)
            rel = here.relative_to(root).parts
            event = rel[0] if rel else ""
            if not event or event.lower() == marker.lower():
                event = root.name       # the root itself is the event folder
            if mode == "all":
                for f in sorted(filenames):
                    if _is_image(f):
                        found.append((here / f, str(root), event, ""))
                continue
            if here.name.lower() != marker.lower():
                continue
            for sub in sorted(dirnames, key=lambda d: wanted.get(d.lower(), 99)):
                if sub.lower() not in wanted:
                    continue
                for sp, _, sfiles in os.walk(here / sub):
                    for f in sorted(sfiles):
                        if _is_image(f):
                            found.append((Path(sp) / f, str(root), event, sub))
            dirnames[:] = []    # an _EXPORT is read through its subfolders only
    return found


# ---------------------------------------------------------------- exiftool

def run_exiftool(paths):
    """One exiftool call per batch through a UTF-8 argfile (Japanese and
    accented paths survive the Windows command line this way)."""
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".args",
                                     delete=False) as af:
        af.write("\n".join(str(p) for p in paths))
        argfile = af.name
    try:
        cmd = [EXIFTOOL, "-charset", "filename=utf8", "-j", "-fast"]
        cmd += [f"-{t}#" if t in NUMERIC else f"-{t}" for t in TAGS]
        cmd += ["-@", argfile]
        out = subprocess.run(cmd, capture_output=True, check=False)
        if not out.stdout.strip():
            return []
        return json.loads(out.stdout.decode("utf-8", errors="replace"))
    finally:
        os.unlink(argfile)


def read_exif(found):
    info = {os.path.normcase(os.path.normpath(str(p))): (r, e, s)
            for p, r, e, s in found}
    rows = []
    paths = [p for p, *_ in found]
    for i in range(0, len(paths), EXIFTOOL_BATCH):
        for r in run_exiftool(paths[i:i + EXIFTOOL_BATCH]):
            key = os.path.normcase(os.path.normpath(r.get("SourceFile", "")))
            root, event, sub = info.get(key, ("", "", ""))
            r.update(Root=root, Event=event, Subfolder=sub)
            rows.append(r)
        print(f"  read {min(i + EXIFTOOL_BATCH, len(paths))}/{len(paths)}",
              end="\r", file=sys.stderr)
    print(file=sys.stderr)
    return rows


# ---------------------------------------------------------------- analysis

def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def lens_name(r):
    for key in ("LensModel", "LensID", "Lens"):
        v = str(r.get(key) or "").strip()
        if v.lower() not in BLANK_LENS:
            return v
    # A manual lens (no CPU) reports "-- mm f/--"; the raw converter may
    # still have written the lens data you typed in as LensInfo.
    manual = any(str(r.get(k) or "").strip() == "-- mm f/--"
                 for k in ("LensModel", "LensID", "Lens"))
    info = str(r.get("LensInfo") or "").strip()
    if info and info.lower() not in BLANK_LENS:
        return f"{info} (manual)" if manual else info
    if manual:
        return f"(manual lens) {r.get('Model') or '?'}"
    return f"(no lens info) {r.get('Model') or '?'}".strip()


def dedupe(rows, subfolders):
    """Keep one row per shot. A shot exported to TIFF16 and sRGB has the same
    camera, serial and capture time; the copy from the subfolder listed
    first in `subfolders` wins, and a tag it lacks is taken from another
    copy. Rows without a capture time are kept."""
    rank = {s.lower(): i for i, s in enumerate(subfolders)}

    def rank_of(r):
        return rank.get(str(r.get("Subfolder", "")).lower(), 99)

    groups = defaultdict(list)
    keep = []
    for r in rows:
        dto = str(r.get("DateTimeOriginal") or "").strip()
        if not dto:
            keep.append(r)
            continue
        key = (r.get("Model"), str(r.get("SerialNumber") or ""), dto,
               str(r.get("SubSecTimeOriginal") or ""))
        groups[key].append(r)
    size_tags = {"ImageWidth", "ImageHeight"}   # belong to the chosen file
    for copies in groups.values():
        copies.sort(key=rank_of)
        best = copies[0]
        for other in copies[1:]:
            for t in TAGS:
                if t not in size_tags and best.get(t) in (None, "") \
                        and other.get(t) not in (None, ""):
                    best[t] = other[t]
        keep.append(best)
    return keep


def _scale(r):
    s = _num(r.get("ScaleFactor35efl"))
    if s:
        return s
    fl, fl35 = _num(r.get("FocalLength")), _num(r.get("FocalLengthIn35mmFormat"))
    return fl35 / fl if fl and fl35 else None


def compute_effective(rows, native_sizes=None, max_zoom=None, tol=None):
    """Add Lens, Scale, Native, NativeFrom, Zoom, AspectChanged and EFL35 to
    every row (in place).

    Zoom = how much the export was cropped compared with the full frame,
    taking the smaller of the short-side and long-side ratios (so a 3:2 ->
    1:1 crop is not mistaken for a zoom). EFL35 = FocalLength x Scale x Zoom.

    The full frame comes from `native_sizes` (the full sensor) or, for a
    model not listed, from the largest export of that model. A shot whose
    35 mm scale is larger than the model's usual one was taken in an
    in-camera crop mode (DX on an FX body, a phone's tele camera): its full
    frame is the native size divided by the extra crop.
    """
    native_sizes = NATIVE_SIZES if native_sizes is None else native_sizes
    max_zoom = MAX_PLAUSIBLE_ZOOM if max_zoom is None else max_zoom
    tol = ASPECT_CHANGE_TOLERANCE if tol is None else tol

    for r in rows:
        r["LensName"] = lens_name(r)
        r["Scale"] = _scale(r)

    # The usual scale of each model = the smallest one seen (its widest frame).
    base = {}
    for r in rows:
        s = r["Scale"]
        if s:
            m = r.get("Model")
            base[m] = min(base.get(m, s), s)

    def group_of(r):
        # FocalLengthIn35mmFormat is a whole number, so a zoom compact's
        # scale wobbles a few % from one focal length to the next: below
        # +8 % it is the same frame, above it an in-camera crop.
        s, b = r["Scale"], base.get(r.get("Model"))
        extra = s / b if s and b else 1.0
        return (r.get("Model"), 1.0 if extra < 1.08 else round(extra, 1))

    observed = defaultdict(lambda: (0, 0))
    for r in rows:
        w, h = _num(r.get("ImageWidth")), _num(r.get("ImageHeight"))
        if w and h:
            g = group_of(r)
            lo, sh = observed[g]
            observed[g] = (max(lo, max(w, h)), max(sh, min(w, h)))

    for r in rows:
        r.update(Native="", NativeFrom="", Zoom=None, AspectChanged=False,
                 EFL35=None)
        fl, w, h = _num(r.get("FocalLength")), _num(r.get("ImageWidth")), \
            _num(r.get("ImageHeight"))
        if not (fl and w and h):
            continue
        model, extra = group_of(r)
        if model in native_sizes:
            nl, ns = native_sizes[model]
            nl, ns, src = nl / extra, ns / extra, "table"
        else:
            nl, ns = observed[(model, extra)]
            src = "largest export"
        r["Native"], r["NativeFrom"] = f"{round(nl)}x{round(ns)}", src
        crop_long, crop_short = nl / max(w, h), ns / min(w, h)
        zoom = max(1.0, min(crop_long, crop_short))
        r["AspectChanged"] = (abs(crop_long - crop_short)
                              / max(crop_long, crop_short)) > tol
        r["Zoom"] = round(zoom, 3)
        if r["Scale"] and zoom <= max_zoom:
            r["EFL35"] = round(fl * r["Scale"] * zoom, 1)
    return rows


# ---------------------------------------------------------------- printing

def _fmt_mm(v):
    return f"{v:g}"


def _bar(n, top, width=30):
    return "#" * max(1, round(width * n / top)) if top and n else ""


def print_counts(title, counter, total, limit=None):
    print(f"\n== {title} ==")
    items = counter.most_common(limit)
    if not items:
        print("  (none)")
        return
    top = items[0][1]
    for name, n in items:
        print(f"{n:7d}  {100 * n / total:5.1f}%  {_bar(n, top, 20):20}  {name}")


def print_focal_by_lens(rows, min_photos=1):
    print("\n== Focal length per lens (as reported by the lens) ==")
    by_lens = defaultdict(Counter)
    for r in rows:
        fl = _num(r.get("FocalLength"))
        if fl:
            by_lens[r["LensName"]][round(fl, 1)] += 1
    for lens, c in sorted(by_lens.items(), key=lambda kv: -sum(kv[1].values())):
        n = sum(c.values())
        if n < min_photos:
            continue
        print(f"\n  [{lens}]  {n} photos")
        if len(c) == 1:
            print(f"    {_fmt_mm(next(iter(c)))} mm")
            continue
        top = max(c.values())
        for fl in sorted(c):
            print(f"    {_fmt_mm(fl):>6} mm  {c[fl]:6d}  {_bar(c[fl], top)}")


def print_effective(rows, bin_mm=None):
    bin_mm = EFL_BIN_MM if bin_mm is None else bin_mm
    print(f"\n== Effective focal length (35 mm equivalent, after your crop; "
          f"{bin_mm} mm bins) ==")
    c = Counter()
    zooms = defaultdict(list)
    for r in rows:
        if r["EFL35"]:
            b = round(r["EFL35"] / bin_mm) * bin_mm
            c[b] += 1
            zooms[b].append(r["Zoom"])
    done = sum(c.values())
    if not done:
        print("  (no photo with enough EXIF)")
    else:
        top = max(c.values())
        print(f"  {'EFL':>8}  {'photos':>6}  {'crop zoom':>11}")
        for b in sorted(c):
            zs = zooms[b]
            lo, hi = f"{min(zs):.2f}", f"{max(zs):.2f}"
            z = f"{lo}x" if lo == hi else f"{lo}-{hi}x"
            print(f"  {b:>5g} mm  {c[b]:6d}  {z:>11}  {_bar(c[b], top)}")
    no_scale = sum(1 for r in rows if r["Zoom"] is not None and not r["Scale"])
    resized = sum(1 for r in rows if r["Zoom"] and r["Zoom"] > MAX_PLAUSIBLE_ZOOM
                  and r["Scale"])
    no_data = sum(1 for r in rows if r["Zoom"] is None)
    aspect = sum(1 for r in rows if r["EFL35"] and r["AspectChanged"])
    print(f"  counted: {done} of {len(rows)}")
    if aspect:
        print(f"  {aspect} with a changed aspect ratio (zoom = the smaller "
              f"side ratio)")
    if resized:
        print(f"  left out: {resized} with a crop zoom above "
              f"{MAX_PLAUSIBLE_ZOOM:g}x (probably a resized export)")
    if no_scale:
        print(f"  left out: {no_scale} without a 35 mm scale in the EXIF")
    if no_data:
        print(f"  left out: {no_data} without focal length or image size")
    guessed = Counter(r.get("Model") for r in rows
                      if r["NativeFrom"] == "largest export")
    if guessed:
        print("  full frame taken from the largest export (model not in "
              "NATIVE_SIZES): " + ", ".join(sorted(str(m) for m in guessed)))


def _aperture_bin(fn):
    stops = [1.4, 2, 2.8, 4, 5.6, 8, 11, 16]
    label = "< f/1.4"
    for s in stops:
        if fn >= s * 0.95:
            label = f"f/{s:g}"
    return label


def print_lens_grid(rows, text, quiet_if_none=False):
    sel = [r for r in rows if text.lower() in r["LensName"].lower()
           and _num(r.get("FocalLength")) and _num(r.get("FNumber"))]
    if not sel and quiet_if_none:
        return
    print(f"\n== Lens matching '{text}': {len(sel)} photos ==")
    if not sel:
        return
    lenses = sorted({r["LensName"] for r in sel})
    if len(lenses) > 1:
        print("  lenses: " + "; ".join(lenses))
    fls = sorted({round(_num(r["FocalLength"])) for r in sel})
    if len(fls) <= 8:
        edges = [(f, f) for f in fls]
    else:
        lo, hi = fls[0], fls[-1]
        step = (hi - lo) / 5
        cuts = [round(lo + step * i) for i in range(6)]
        edges = [(cuts[i] + (i > 0), cuts[i + 1]) for i in range(5)]

    def fbin(fl):
        fl = round(fl)
        for a, b in edges:
            if a <= fl <= b:
                return f"{a}mm" if a == b else f"{a}-{b}mm"
        return f"{fl}mm"

    grid = Counter((fbin(_num(r["FocalLength"])), _aperture_bin(_num(r["FNumber"])))
                   for r in sel)
    rows_ = [f"{a}mm" if a == b else f"{a}-{b}mm" for a, b in edges]
    order = ["< f/1.4", "f/1.4", "f/2", "f/2.8", "f/4", "f/5.6", "f/8",
             "f/11", "f/16"]
    cols = [a for a in order if any(grid[(f, a)] for f in rows_)]
    print(f"  {'':>11}" + "".join(f"{a:>8}" for a in cols) + f"{'total':>8}")
    for f in rows_:
        tot = sum(grid[(f, a)] for a in cols)
        print(f"  {f:>11}" + "".join(f"{grid[(f, a)] or '.':>8}" for a in cols)
              + f"{tot:>8}")
    print("  (aperture columns are full stops: f/4 = f/4 up to just under "
          "f/5.6)")


def report(rows, lens_text=None, title=None, compact=False):
    if title:
        print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")
    total = len(rows)
    print(f"\n{total} photos")
    if not total:
        return
    print_counts("Photos per camera", Counter(str(r.get("Model") or "?")
                                             for r in rows), total)
    print_counts("Photos per lens", Counter(r["LensName"] for r in rows), total)
    if not compact:
        print_focal_by_lens(rows)
    print_effective(rows)
    if lens_text:
        print_lens_grid(rows, lens_text, quiet_if_none=compact)


# ---------------------------------------------------------------- CSV

def write_csv(rows, path):
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS + REPORT_FIELDS,
                           extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k))
                        for k in CSV_FIELDS + REPORT_FIELDS})


def read_csv(path):
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return [{k: v for k, v in r.items() if k in CSV_FIELDS}
                for r in csv.DictReader(fh)]


# ---------------------------------------------------------------- main

def main(argv=None):
    # Folder names in Japanese/accents must not crash a redirected run
    # (Windows pipes default to cp1252).
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(
        description="Which focal lengths, lenses and apertures you really use "
                    "(from the EXIF of your exported photos).")
    ap.add_argument("--roots", nargs="+", default=None,
                    help="folders to scan (default: ROOTS at the top of the "
                         "script, or the current folder)")
    ap.add_argument("--all-images", action="store_true",
                    help="read every image under the roots, not only the "
                         f"{EXPORT_MARKER}\\<subfolder> exports")
    ap.add_argument("--subfolders", default=None,
                    help="export subfolders, best first, ';'-separated "
                         f"(default: {';'.join(EXPORT_SUBFOLDERS)})")
    ap.add_argument("--lens", help="aperture x focal length grid for the "
                                   "lenses whose name contains this text")
    ap.add_argument("--by-folder", action="store_true",
                    help="also print a short report per event folder (the "
                         "first folder below each root)")
    ap.add_argument("--csv", help="write every photo (EXIF + computed "
                                  "columns) to this CSV")
    ap.add_argument("--from-csv", help="analyse a CSV written by --csv "
                                       "instead of running exiftool")
    args = ap.parse_args(argv)

    subfolders = ([s.strip() for s in args.subfolders.split(";") if s.strip()]
                  if args.subfolders else EXPORT_SUBFOLDERS)

    if args.from_csv:
        rows = read_csv(args.from_csv)
        print(f"{len(rows)} photos read from {args.from_csv}")
    else:
        roots = args.roots or ROOTS or [os.getcwd()]
        mode = "all" if args.all_images else FOLDER_MODE
        found = find_photos(roots, mode, EXPORT_MARKER, subfolders)
        where = ("every image" if mode == "all" else
                 f"{EXPORT_MARKER}\\{{{', '.join(subfolders)}}}")
        print(f"{len(found)} files found ({where}) under: " + ", ".join(roots))
        if not found:
            return 1
        rows = read_exif(found)
        before = len(rows)
        rows = dedupe(rows, subfolders)
        if before != len(rows):
            print(f"{before - len(rows)} duplicate exports of the same shot "
                  f"counted once")

    compute_effective(rows)

    if args.csv:
        write_csv(rows, args.csv)
        print(f"CSV written: {os.path.abspath(args.csv)}")

    report(rows, args.lens)

    if args.by_folder:
        groups = defaultdict(list)
        for r in rows:
            groups[(r.get("Root", ""), r.get("Event", ""))].append(r)
        for (root, event), grp in sorted(groups.items()):
            report(grp, args.lens, title=f"{event or '(root)'}   [{root}]",
                   compact=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
