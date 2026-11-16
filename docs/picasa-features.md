# Picasa 3.x feature inventory and roadmap

Google Picasa (desktop, 2002–2016; last version 3.9.141) was a free Windows/Mac photo organizer.
This page lists what it did, how much of each feature Photo Organizer covers today, and a
suggested order for adding the rest.

Legend: ✅ done · 🟡 partial · ⬜ not started

## 1. Library and organization

| Picasa feature | Notes | Status |
|---|---|---|
| Watched folders / Folder Manager | Picks which disk folders to scan, and keeps watching them for changes | ✅ Folder Manager dialog. Changes are found with **Rescan**, not live file watching |
| Folder list grouped by year | Flat list of folders with counts, under year headings | ✅ future dates count as "Year unknown", listed last |
| Folder tree view | Alternative hierarchical view (View → Folder View → Tree) | ⬜ |
| Library thumbnail grid ("lightbox") | Thumbnail size slider, drop shadows, hover info | ✅ |
| Albums (virtual) | Collections that don't move files; one photo can be in several albums | ⬜ — **high value** |
| Stars / favorites | Star a photo; *Starred Photos* album; "show starred only" filter | ⬜ — **high value, cheap** |
| Captions and folder descriptions | "Add a description" under a folder title; per-photo captions (stored in IPTC for JPEGs) | ⬜ |
| Tags (keywords) | Free-text tags, Quick Tags buttons, tag search | ⬜ |
| Search box | Instant search over filenames, captions, tags, people, folder names and camera info | 🟡 filename filter, plus the ✦ box: people by name (`@Name`, several at once, "and"/"or"), oldest/newest, and a description |
| Filters bar | Show only starred / video / faces / geotagged items, plus a minimum-date slider | ⬜ |
| Timeline view | Browse all folders chronologically with a date scrubber | ⬜ |
| Hide folders / hide photos | Remove items from view without deleting them | ⬜ |
| Move / rename / delete files | File operations from inside the app, with the Recycle Bin | ⬜ |
| Batch rename, batch edit | Rename or rotate many items at once ("Picture → Batch Edit") | ⬜ |
| Duplicate detection | Find exact and near-duplicate photos | ⬜ (perceptual hash) |
| Import | Copy from a camera or SD card into dated folders | ⬜ (out of scope while PhotoSync does this) |
| Projects / Photo Tray | Temporary "hold" basket to collect photos for an action (export, collage, email) | ⬜ |

## 2. People (name tags)

| Picasa feature | Notes | Status |
|---|---|---|
| Automatic face detection | Runs in the background over the whole library | ✅ OpenCV YuNet detector; scans the folder you are viewing first. Videos: a frame near the start and one near the end |
| Face recognition / grouping | Clusters similar faces into `<Unknown Person>` groups | ✅ SFace embeddings and greedy clustering |
| Name a face in the viewer | Name box under each face, with autocomplete | ✅ |
| Suggested matches | "Is this *X*?" with ✓/✗ for each suggestion; batch confirm | ✅ per-person suggestion strip (height-capped, can be hidden) and per-face suggestion in the viewer |
| A person's photos | Album of everyone tagged, newest first | ✅ grouped under year headings, with date and file name under each picture; future dates go under "Year unknown", last |
| Manually add a face | Draw a box where detection missed someone | ✅ "Tag a person" |
| Ignore a face | Hide faces you don't care about (strangers, posters) | ✅ |
| Who appears with whom | People who are often photographed together | ✅ "Often with" next to the name on a person's page (top five, clickable) |
| Merge / rename / delete a person | Renaming onto an existing name merges the two | ✅ |
| Choose a person's thumbnail | "Set as thumbnail" | 🟡 the API supports `cover_face`; no UI yet |
| People as contacts | Link a person to Google Contacts or an email address | ⬜ |
| Face Movie | Slideshow that zooms and aligns on a chosen person's face over time | ⬜ fun, and the face boxes already make it possible |
| Write face tags to files | `.picasa.ini` `faces=rect64(…),contactId`; later XMP-MWG regions | ⬜ export to XMP for portability (see §6) |

## 3. Viewer

| Picasa feature | Notes | Status |
|---|---|---|
| Full-screen viewer with filmstrip | Arrow-key navigation, filmstrip at bottom | ✅ (in the right pane; ⬜ true full-screen with F11-style toggle) |
| Zoom / pan | Mouse wheel, zoom slider, 1:1 | ✅ |
| Video playback | Plays MOV/MP4/AVI and others | ✅ in the viewer, with a control bar that stays upright when rotated. Formats the browser can't play are converted with ffmpeg and cached |
| Slideshow | Timed slideshow with transitions and music | ⬜ — **cheap, high value** |
| Side-by-side compare | Original next to edited, or two photos | ⬜ |
| EXIF / properties panel | Camera, lens, exposure, histogram | 🟡 date, size and path only |
| Map pane | Shows where geotagged photos were taken | ⬜ |

## 4. Editing (non-destructive)

Picasa never changed originals until you chose **Save**. Until then, edits lived in `.picasa.ini`.
On Save it kept the original in a hidden `.picasaoriginals` folder, so **Undo Save / Revert** always worked.

| Picasa feature | Notes | Status |
|---|---|---|
| Rotate left / right | Also from the library (multi-select) | ✅ in the viewer; ⬜ from the library |
| Crop | Free or fixed ratios (4×6, 5×7, 8×10, square, …), plus "Manual" | ✅ |
| Straighten | Fine-angle rotation with a grid overlay | ⬜ |
| Red-eye removal | Automatic, or click on an eye | ⬜ |
| I'm Feeling Lucky | One-click auto color and contrast | ⬜ (simple auto-levels) |
| Auto Contrast / Auto Color | Separate one-click fixes | ⬜ |
| Fill light / highlights / shadows / color temperature | "Tuning" tab sliders | ⬜ (CSS filters for preview, Pillow for render) |
| Retouch | Healing brush for blemishes | ⬜ (OpenCV `inpaint`) |
| Text overlay | Add captions on top of an image | ⬜ |
| Effects (~24) | Sharpen, sepia, B&W, warmify, film grain, tint, saturation, soft focus, glow, filtered B&W, focal B&W, graduated tint, vignette, duo-tone, borders, cross-process, Lomo-ish, HDR-ish, … | ⬜ |
| Undo / redo of edit stack | Per-photo edit history | 🟡 "Reset edits" only |
| Save / Save As / Export | Write edited JPEG; keep the original | 🟡 "Save copy" writes `name_edited.jpg` (or `name_edited.mp4` for videos) next to the original |
| Batch edit | Apply rotate, auto-fix or effects to a selection | ⬜ |
| Video trim | Trim the start and end of clips (Picasa 3.x) | ⬜ (needs ffmpeg) |

## 5. Create / share

| Picasa feature | Status |
|---|---|
| Collage (picture pile, mosaic, frame mosaic, grid, contact sheet, multiple exposure) | ⬜ |
| Movie Maker (slides plus video, audio track, transitions, title text) | ⬜ |
| Gift CD / Export folder at reduced size | ⬜ export selection, resized, to a folder or zip |
| Print layouts (contact sheet, 4×6 per page) | ⬜ browser print CSS |
| Email photos (auto-resize) | ⬜ |
| Web album sync, blogging, Google+ upload | ✖ not planned (services gone) |
| Screen saver / desktop background | ✖ not applicable |
| Geotagging with Google Earth / Places | ⬜ read GPS from EXIF and show it on a Leaflet map; set location manually |

## 6. Data / interoperability ideas

- **Read existing `.picasa.ini` files.** Old Picasa folders contain them. Importing would bring back stars, captions,
  album membership, crops and rotations, and face tags (`faces=rect64(hex),contactId;…` mapped through the
  `[Contacts2]` section). The 64-bit `rect64` value splits into four 16-bit numbers: left, top, right, bottom,
  each divided by 65535.
- **Write XMP sidecars.** Writing XMP (MWG Regions for faces, `xmp:Rating` for stars, `dc:subject` for tags) would make
  the work portable to Lightroom, digiKam and Windows Photos.
- **Database backup.** Everything Photo Organizer knows is in `data/library.db`. Exporting it gives a simple backup.

## Suggested next steps (by value ÷ effort)

1. Stars, plus a "Starred" filter and album.
2. Slideshow (full screen, keyboard).
3. Multi-select in the grid (Ctrl/Shift-click), with batch rotate, star and export.
4. Albums (virtual collections) in the left pane.
5. I'm Feeling Lucky, auto-contrast and basic tuning sliders.
6. EXIF panel, GPS, and a map view.
7. Captions, tags, and full search.
8. Import from `.picasa.ini`; export XMP.
9. Timeline view.
10. Collage, Movie Maker and Face Movie.

## Sources

- [Expert Reviews — Google Picasa 3.9 review](https://www.expertreviews.co.uk/software/52152/google-picasa-39-review)
- [Softpedia — Picasa changelog](https://www.softpedia.com/progChangelog/Picasa-Changelog-6962.html)
- [Picasa Resources — How Picasa works](https://www.sites.google.com/site/picasaresources/picasa/how-picasa-works)
- [Picasa Resources — Where Picasa stores face tags](https://www.sites.google.com/site/picasaresources/picasa/where-does-picasa-store-facetags)
- [ExifTool picasa_faces.config](https://sources.debian.org/src/libimage-exiftool-perl/12.16%2Bdfsg-2/config_files/picasa_faces.config)
- [.picasa.ini reverse-engineering notes (gist)](https://gist.github.com/1073823)
- [Digital Photography School — Getting to know Picasa](https://digital-photography-school.com/getting-to-know-picasa-free-image-editor-browser-google/)
- [Tom's Guide — Picasa 3 review](https://tomsguide.com/us/google-picasa,review-2619.html)
- [Laptop Mag — Google Picasa 3 review](https://www.laptopmag.com/reviews/software/google-picasa-3)
- [Digit — How to use Picasa 3 better](https://www.digit.in/how-to/general/tips-tricks-picasa-3-2806.html)
- [AddictiveTips — 12 features of Picasa](https://www.addictivetips.com/windows-tips/12-features-of-picasa-that-you-probably-dont-know-about)
- [Macworld/MacRumors — Picasa 3.5 adds face recognition](https://macrumors.com/2009/09/23/google-releases-picasa-3-5-with-facial-recognition-and-improved-geotagging)
