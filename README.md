# Photo Organizer

A Picasa-style photo and video organizer that runs locally and is used in your web browser.

- **Left pane:** **People** (named people, plus `<Unknown Person>` groups of similar faces) and **Folders** (grouped by year, with counts).
- **Right pane:** thumbnail library, and a photo/video viewer with zoom, pan, rotate and crop.
- **Describe-a-photo search:** type what you remember ("kids opening presents", "sunset at the beach") in the ✦ box. An AI image model ranks your photos and video frames by how well they match. It runs on your computer, so nothing is uploaded.
- **Faces:** detected automatically in the background. Name a face in the viewer, and the app suggests other photos of the same person to confirm (✓) or reject (✗).

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
| `static/` | Single-page UI (vanilla JS, no build step) |

`data/` holds the database plus thumbnail and face-crop caches. Deleting it resets the app. You will lose
names and edits, but not your photos.

## Notes and limits

- Describe-a-photo search is good at scenes, objects and activities ("birthday cake", "snowy street", "people on a boat").
  It does not know who people are. Use People for that. It also cannot read dates or places.
- The first face scan of a large library takes a while (about 0.3–0.5 s per photo over `/mnt/c`). The folder you have open is scanned first.
- Videos play in the viewer with their own control bar (Space plays and pauses, and a click on the picture does the same). The bar stays upright when you rotate a video.
- Formats the browser can't play (iPhone HEVC `.mov`, AVI, WMV and others) are converted to H.264 MP4 the first time you open them.
  The converted copy is cached in `data/video/`, and the original is not touched. This needs ffmpeg, either on `PATH` or from `pip install imageio-ffmpeg`.
- Rotating or cropping a video is saved like a photo edit. **Save copy** writes `name_edited.mp4` next to the original, and this also needs ffmpeg.

See [docs/picasa-features.md](docs/picasa-features.md) for the Picasa feature inventory and roadmap.
