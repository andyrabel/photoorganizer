# Photo Organizer

A Picasa-style photo and video organizer that runs locally and is used in your web browser.

- **Left pane:** **People** (named people, plus `<Unknown Person>` groups of similar faces) and **Folders** (grouped by year, with counts).
- **Right pane:** thumbnail library, and a photo/video viewer with zoom, pan, rotate and crop.
- **Describe-a-photo search:** type what you remember ("kids opening presents", "sunset at the beach") in the ✦ box. An AI image model ranks your photos and video frames by how well they match. It runs on your computer, so nothing is uploaded.
- **Faces:** detected automatically in the background, in photos and in videos (a frame near the start and one near the end). Name a face in the viewer, and the app suggests other photos of the same person to confirm (✓) or reject (✗). On a person's page the suggestions take up at most about half the screen (scroll within them), and **Hide suggestions** folds them away so you can see that person's photos; **Show suggestions** brings them back. The choice is remembered. A person's photos are shown newest first under year headings, with the date and file name under each picture.
- **Dates in the future** (usually a camera whose clock was never set) are treated as unknown: those photos are listed under **Year unknown**, after all the real years, both on a person's page and in the folder list.
- **Photos / Videos switch** (bottom of the library): show everything, only photos, or only videos. It applies to folders, people, search results and the tools below.
- **Folders** can be collapsed by year: click a year heading. **Collapse all**, **Expand all** and **Jump to…** are at the top of the list.
- **Year check:** finds photos and videos that seem to be in the wrong year folder (any folder path with a year in it, such as `PhotoSync/2019/11`). It also makes a best guess, with a confidence percentage, for every folder that is *not* in a year folder (`No Year`, `Angie's Early Years`, `Camera Roll` and the like), and flags event folders whose name gives the wrong year or month. Selected files and folders can then be moved (or renamed) to match.
- **Junk:** finds pictures that are not personal photos: screenshots, receipts and paperwork, memes and web graphics. Drawings, paintings and book covers are listed separately as possible **book art**. They are never pre-selected, and can be marked so they are never removed.
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

On first run, the app watches `C:\Users\<you>\Pictures` and, if there is one, `C:\Users\<you>\OneDrive\Pictures`.
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

### Files in a year folder

The year check looks at every folder whose path below a watched folder has a year segment, such as `2019` in
`PhotoSync/2019/11` or `2008/0609 SW Nevada move out`, at any depth. For each file it weighs this evidence:

| Evidence | Weight |
|---|---|
| Camera date (EXIF date taken, with a camera make or model) | strong |
| Video recording date (MP4/QuickTime header) | strong |
| Date in the file name (`IMG_20180812_…`, `2018-08-12 …`, older phones' `07-29-08_1828`) | good |
| Year in an event folder's name (`…/2019/11/Birthday Nov 2011`) | fair |
| Year written in the file name (`Emmas Bday Jun-1995.jpg`) | fair |
| Most other files in the same folder (used only for files without their own date) | good |
| EXIF "last saved" date only (no date taken) | weak; often when the picture was edited or copied |
| Scanner date (scanner make or scanning software in the EXIF) | very weak; when it was scanned, not taken |
| A digital date ten or more years after a pre-1998 year folder | very weak; a print that was scanned or photographed later |
| File modified date | weak; often just the date the file was copied, so it never suggests a year on its own |

Confidence is **High** (75% or more), **Medium** (50% or more) or **Low**. It is lower when some evidence agrees with the
folder, and when the date is within a day of New Year (video dates are stored in UTC). High-confidence suggestions
are selected for you. **Move** swaps the year folder (and a month folder right below it) for the suggested one,
keeps any event folder below that, and never overwrites a file. People, edits and search data move with the file.
Folders left empty are removed, but the year folder itself is kept.

### Folders that are not in a year folder

Every folder outside the year folders, at any depth, gets a **best-guess year with a confidence percentage**. A
subfolder counts with the nearest folder above it that has pictures of its own. A folder that holds year folders
(such as `PhotoSync`) is a container and is not guessed. The guess combines:

- the dates of the files in it, weighted as above. The more files that agree, the surer the guess, and a folder
  whose files span several years gets a lower confidence;
- a year in the folder's name (`Wangs July 2014`), or a decade or range (`1980s`, `Helsby Grammar School 1975-1982`),
  which limits the guess to those years (dates outside them are taken to be scan dates);
- when nothing else is known, the files' modified dates (shown as **≈** and always low confidence).

What you can do with it:

- **Move** the whole folder, with its subfolders, into the year folder: `<year folders>/<year>[/<month>]/<folder name>`, following the
  layout most of that watched folder already uses (for example `PhotoSync/2014/07/...` or `2014/...`).
- **Sort into year folders** is offered instead for a folder whose files come from many years (a phone's camera roll, an
  iCloud export). Each file with a reliable date (camera or file-name date) goes to `<year>[/<month>]/<folder name>/`.
  Files without one stay where they are.
- A folder with no dates at all is listed with no guess, and nothing is offered.

### Folder names that disagree with their photos

An event folder inside a year folder whose name gives a year or month that at least 80% of its camera dates disagree with
is listed with a corrected name (`Birthday Nov 2011` → `Birthday Nov 2019`, `Sydney Oct 2026` → `Sydney Sep 2026`). The
way the month was written (`Sept`, `Sep`, `September`) is kept. Folder moves and renames are never pre-selected.

## Junk

The junk finder compares each picture's describe-a-photo embedding with short descriptions of each kind of picture (zero-shot
CLIP). It also uses clues the picture itself can't show:

| Kind | Also uses |
|---|---|
| Screenshots | the iPhone's own "Screenshot" EXIF mark, phone and tablet screen sizes with no camera data, names like `Screen Shot …`, a `Screenshots` folder |
| Documents, receipts & signs | (picture content only) |
| Memes, logos & web graphics | no camera data, `.gif`/`.bmp`, folders such as `Backgrounds` or `Smileys` |
| Illustrations & book art | drawings, paintings, cartoons, book covers and picture-book pages |

Every junk score is lowered when the picture also looks like a personal photo (a video-call screenshot of family), when a
camera took it (a photo of a sign is less likely to be junk than a downloaded graphic), and when named people are in it.
Only high-confidence screenshots and web graphics are pre-selected. Documents are left for you, because some are keepsakes.

- **Keep (not junk)** remembers the decision, so the picture is not suggested again.
- **Mark as book art** protects a picture for good: the server refuses to remove it, and it stays listed under
  *Illustrations & book art* so all your artwork can be found in one place.
- **Remove** moves pictures to a hidden `.removed-junk` folder inside the watched folder. **Put them all back** and
  **Delete them permanently** work as they do for duplicates.

The junk finder only sees pictures the describe-a-photo search has analyzed. The header says how many are still waiting.

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
| `tidy.py` | Year check (date evidence, confidence, moving files, folder guesses, moves and renames) and duplicate finding (content hash, difference hash, holding folder) |
| `junk.py` | Junk finder: zero-shot CLIP categories plus file clues, keep / book-art marks |
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
- `.heic` photos are not indexed yet.
- Rotating or cropping a video is saved like a photo edit. **Save copy** writes `name_edited.mp4` next to the original, and this also needs ffmpeg.

See [docs/picasa-features.md](docs/picasa-features.md) for the Picasa feature inventory and roadmap.
