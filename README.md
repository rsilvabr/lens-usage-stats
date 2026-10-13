# lens-usage-stats

Which focal lengths, lenses and apertures do you **really** use — counted from the photos you actually exported, not from every frame you shot.

One Python script, no dependencies besides [exiftool](https://exiftool.org).

## Why another focal length counter

Most focal-length statistics read the EXIF of every shot. That counts the outtakes, and it trusts the number the lens reported. This script does two things differently:

1. **It reads your exports** (by default the Capture One style `_EXPORT\<subfolder>\` folders), so the statistics describe the keepers.
2. **It measures the effective focal length after your crop.** A 100 mm shot cropped to half its width frames the scene like a 200 mm lens. The script compares each export's size with the camera's full frame and reports the 35 mm equivalent focal length you actually ended up with — the number that matters when you wonder whether a longer lens would be worth it.

## Example output

Three event folders, 310 photos (excerpt):

```
== Photos per lens ==
     56   18.1%  ####################  (manual lens) NIKON D700
     54   17.4%  ###################   NIKKOR Z 70-180mm f/2.8
     39   12.6%  ##############        NIKKOR Z 50mm f/1.8 S
     27    8.7%  ##########            28mm f/1.4 (manual)
  ...

== Effective focal length (35 mm equivalent, after your crop; 5 mm bins) ==
       EFL  photos    crop zoom
     30 mm      70  1.00-1.15x  ##############################
     35 mm      11  1.00-1.19x  #####
     50 mm      44  1.00-1.04x  ###################
     85 mm      52  1.00-1.02x  ######################
     90 mm      10  1.03-1.09x  ####
    100 mm       5  1.08-1.18x  ##
    110 mm       1       1.26x  #
  ...
  counted: 310 of 310
  4 with a changed aspect ratio (zoom = the smaller side ratio)
```

With `--lens 70-180`, a focal length × aperture grid for that lens:

```
== Lens matching '70-180': 54 photos ==
                f/2.8     f/4   f/5.6     f/8    f/11    f/16   total
      70-92mm      10       9      10       4       2       5      40
     93-114mm       .       .       .       .       .       .       0
    115-136mm       1       1       2       .       .       1       5
    137-158mm       1       .       2       .       .       .       3
    159-180mm       1       1       1       3       .       .       6
```

## Usage

```
python lens_usage_stats.py --roots G:\2024 G:\2025
python lens_usage_stats.py --roots G:\2025 --lens 70-180 --csv exif.csv
python lens_usage_stats.py --from-csv exif.csv --by-folder
```

| Option | What it does |
|---|---|
| `--roots DIR ...` | Folders to scan (default: `ROOTS` in the script, or the current folder) |
| `--all-images` | Read every image under the roots, not only the exports |
| `--subfolders "TIFF16;sRGB"` | Export subfolders to read, best first |
| `--lens TEXT` | Focal length × aperture grid for the lenses whose name contains TEXT |
| `--by-folder` | Also a short report per event folder (the first folder below each root) |
| `--csv FILE` | Write every photo (EXIF + computed columns) to a CSV |
| `--from-csv FILE` | Analyse a CSV written by `--csv` instead of running exiftool — reading a large archive from a hard disk takes a while, re-analysing the CSV takes a second |

## Settings

The top of `lens_usage_stats.py` holds everything you may want to change:

- `ROOTS`, `FOLDER_MODE`, `EXPORT_MARKER` (`_EXPORT`), `EXPORT_SUBFOLDERS`, `EXTENSIONS`
- `EFL_BIN_MM` (5), `MAX_PLAUSIBLE_ZOOM` (3.0), `ASPECT_CHANGE_TOLERANCE` (0.10)
- `NATIVE_SIZES`: the full-sensor image size per camera model (most Nikon bodies and the Fujifilm S3/S5 Pro are listed)

## How the effective focal length is computed

For each photo:

- **Full frame**: the camera's image size from `NATIVE_SIZES`. For a model not listed there, the largest export of that model is used instead — right as long as at least one of its photos was exported uncropped (the report names the models where this happened).
- **Crop zoom**: full frame ÷ export size, for the long and for the short side; the smaller of the two wins. That way a 3:2 → 1:1 crop that keeps the full height counts as an aspect change, not as a zoom.
- **35 mm scale**: exiftool's `ScaleFactor35efl` (or `FocalLengthIn35mmFormat ÷ FocalLength`).
- **Effective focal length** = focal length × 35 mm scale × crop zoom.

Edge cases it handles:

- **In-camera crop modes** (DX mode on an FX body, a phone's second camera): the 35 mm scale is larger than the model's usual one, so the full frame shrinks by the same factor and the crop is not counted twice.
- **Manual (non-CPU) lenses** report `-- mm f/--`: the lens data typed into the raw converter (`LensInfo`) is used when present, otherwise the lens is listed as `(manual lens) <camera>`.
- **The same shot exported several times** (TIFF16 *and* sRGB): counted once, from the first folder in `EXPORT_SUBFOLDERS` — identified by camera, serial number and capture time down to the sub-second. A tag that copy lacks is taken from the other copies.
- **Resized exports**: a crop zoom above `MAX_PLAUSIBLE_ZOOM` is almost certainly a downscaled file, not a crop. Those photos still count for cameras, lenses and focal lengths, but stay out of the effective table. Keep resized export folders (web, mobile) out of `EXPORT_SUBFOLDERS`.

## Limitations

- An export that was cropped **and** resized looks like a bigger crop than it was. The script cannot tell them apart; read your full-size exports.
- Old compacts that write no 35 mm focal length are counted in every table except the effective one.
- Optical corrections in the raw converter (distortion correction crops a few %) show up as a small crop zoom (1.01–1.05×).

## Requirements

- Python 3.9+
- exiftool on the `PATH`
- Tests (optional): `pip install pytest pillow`, then `python -m pytest tests`

## Disclaimer

Made for my personal workflow (with the help of AI assistants). It only reads your photos and never writes to them. If you find a bug, please let me know.

## License

MIT
