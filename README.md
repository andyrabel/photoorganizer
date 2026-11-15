# Photo Organizer

A Picasa-style photo and video organizer that runs locally and is used in your web browser.

- **Left pane:** **People** (named people, plus `<Unknown Person>` groups of similar faces) and **Folders** (grouped by year, with counts).
- **Right pane:** thumbnail library, and a photo/video viewer with zoom, pan, rotate and crop.
- **Describe-a-photo search:** type what you remember ("kids opening presents", "sunset at the beach") in the ✦ box. An AI image model ranks your photos and video frames by how well they match. It runs on your computer, so nothing is uploaded.
- **Faces:** detected automatically in the background, in photos and in videos (a frame near the start and one near the end). Name a face in the viewer, and the app suggests other photos of the same person to confirm (✓) or reject (✗).
- **Photos / Videos switch** (bottom of the library): show everything, only photos, or only videos. It applies to folders, people, search results and the tools below.
- **Year check:** finds photos and videos that seem to be in the wrong year folder (any folder path with a year in it, such as `PhotoSync/2019/11`). Each one gets a suggested year and a confidence rating, with the evidence listed. Selected files can then be moved to the right year folder.
- **Duplicates:** finds exact copies (byte-for-byte identical) and look-alikes (the same picture resized, re-saved or converted). You choose which copies to remove.

Everything stays on your machine. Edits are non-destructive: rotation and crop are stored in the
app's database, and your original files are never modified. **Save copy** writes a new
`name_edited.jpg` next to the original.

## Run

```bash
pip install -r requirements.txt     # Flask, OpenCV, Pillow, NumPy, ONNX Runtime
python3 app.py                      # then open http://localhost:8765
```

Under WSL, open `http://localhost:8765` in a Windows browser (Edge or Chrome).

On first run, the app watches `C:\Users\<you>\Pictures` (it looks for `/mnt/c/Users/*/Pictures`).
To add or remove folders, use **Folder Manager…**. Windows paths such as `D:\Photos` are accepted.

Options: `--port 8765`, `--host 127.0.0.1`, `--workers 2` (the number of background face-scan threads).

The face models (`models/yunet.onnx`, `models/sface.onnx`) come from the
[OpenCV Model Zoo](https://github.com/opencv/opencv_zoo):

```bash
mkdir -p models
curl -L -o models/yunet.onnx https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx
curl -L -o models/sface.onnx https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx
```

Describe-a-photo search uses OpenAI's [CLIP ViT-B/32](https://huggingface.co/Xenova/clip-vit-base-patch32)
model, exported to ONNX (about 600 MB). It is optional. Without these files, the rest of the app works normally:

```bash
mkdir -p models/clip
for f in onnx/vision_model.onnx onnx/text_model.onnx tokenizer.json; do
  curl -L -o models/clip/$(basename $f) https://huggingface.co/Xenova/clip-vit-base-patch32/resolve/main/$f
done
```

After the models are in place, the app analyzes every photo once in the background (about 10 per second).
Search works during that pass, but it only finds photos that have already been analyzed.

## Year check

The year check looks only at folders whose path below a watched folder has a year segment, such as `2019` in
`PhotoSync/2019/11`. For each file it weighs this evidence:

| Evidence | Weight |
|---|---|
| Camera date (EXIF) | strong |
| Video recording date (MP4/QuickTime header) | strong |
| Date in the file name (`IMG_20180812_…`, `2018-08-12 …`) | good |
| Year in an event folder's name (`…/2019/11/Birthday Nov 2011`) | fair |
| Most other files in the same folder (used only for files without their own date) | good |
| File modified date | weak; often just the date the file was copied, so it never suggests a year on its own |

Confidence is **High** (75% or more), **Medium** (50% or more) or **Low**. It is lower when some evidence agrees with the
folder, and when the date is within a day of New Year (video dates are stored in UTC). High-confidence suggestions
are selected for you. **Move** swaps the year folder (and a month folder right below it) for the suggested one,
keeps any event folder below that, and never overwrites a file. People, edits and search data move with the file.
Folders left empty are removed, but the year folder itself is kept.

## Duplicates

- **Exact copies** have identical contents. Only files that share a size are read in full, so this is quick.
- **Look-alikes** have nearly the same 64-bit picture fingerprint (difference hash) and the same shape.
  They are usually resized or re-saved copies, but can also be burst shots, so check before removing. Double-click to open one.

The copy to keep is chosen in this order: highest resolution, a name that doesn't look like a copy (`copy`, `(2)`, `_edited`),
already in the right year folder, then the largest file. Byte-for-byte copies of the kept file are marked for removal; you can change any choice.
**Remove** moves files to a hidden `.removed-duplicates` folder inside the watched folder (on the same drive, so it's instant).
**Put them all back** restores them. **Delete them permanently** empties the folder. The app never deletes a file any other way.

## Viewer keys

| Key | Action |
|---|---|
| ← / → | previous / next |
| + / − / mouse wheel | zoom (drag to pan, double-click toggles zoom) |
| 0 / 1 | fit / actual size |
| [ / ] | rotate left / right |
| C, then Enter | crop, then apply |
| F | show or hide face boxes |
| Esc | back to library (or cancel crop) |

## How it works

| File | Role |
|---|---|
| `app.py` | Flask server and JSON API; serves originals with HTTP Range so videos can seek |
| `library.py` | SQLite index (`data/library.db`), folder walker, background scan workers |
| `semantic.py` | Describe-a-photo search: CLIP image embeddings (background worker), text query embedding, ranking |
| `faces.py` | YuNet face detection, SFace 128-d embeddings, suggestions (cosine ≥ 0.40), `<Unknown Person>` clustering |
| `imaging.py` | EXIF-aware decoding, thumbnails, rendering edits, export, video frames and dates |
| `video.py` | Optional ffmpeg jobs: convert videos for playback, save rotated/cropped video copies |
| `tidy.py` | Year check (date evidence, confidence, moving files) and duplicate finding (content hash, difference hash, holding folder) |
| `static/` | Single-page UI (vanilla JS, no build step) |

`data/` holds the database plus thumbnail and face-crop caches. Deleting it resets the app. You will lose
names and edits, but not your photos.

## Notes and limits

- Describe-a-photo search is good at scenes, objects and activities ("birthday cake", "snowy street", "people on a boat").
  It does not know who people are. Use People for that. It also cannot read dates or places.
- The first face scan of a large library takes a while (about 0.3–0.5 s per photo and 0.8 s per video over `/mnt/c`). The folder you have open is scanned first.
- In a video, a face's box shows only while the video is paused on the frame where the face was found. Click the face in the side panel to jump there.
  Someone who appears only in the middle of a clip won't be found.
- Videos play in the viewer with their own control bar (Space plays and pauses, and a click on the picture does the same). The bar stays upright when you rotate a video.
- Formats the browser can't play (iPhone HEVC `.mov`, AVI, WMV and others) are converted to H.264 MP4 the first time you open them.
  The converted copy is cached in `data/video/`, and the original is not touched. This needs ffmpeg, either on `PATH` or from `pip install imageio-ffmpeg`.
- Rotating or cropping a video is saved like a photo edit. **Save copy** writes `name_edited.mp4` next to the original, and this also needs ffmpeg.

See [docs/picasa-features.md](docs/picasa-features.md) for the Picasa feature inventory and roadmap.
