# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A local, Picasa-style photo/video organizer: a Flask server (`app.py`) plus a single-page vanilla-JS UI in `static/` (no build step). It runs under WSL and is used from a Windows browser at `http://localhost:8765`. User-facing behaviour is documented in detail in `README.md`; the feature roadmap is `docs/picasa-features.md`. Commits that add or change features update both, so keep them in sync.

## Commands

```bash
pip install -r requirements.txt
python3 app.py                      # --port 8765 --host 127.0.0.1 --workers 2 (face-scan threads)
python3 -m py_compile *.py          # quick syntax check
```

There is no test suite, linter config or build. Verify changes by running the app and exercising the feature in the browser. Model downloads (YuNet/SFace face models in `models/`, optional CLIP ONNX in `models/clip/`) are in the README; without the CLIP files, search and the junk finder are disabled but everything else works.

`data/` (git-ignored) holds `library.db` (SQLite, WAL), `config.json` (watched roots), and thumbnail / face-crop / converted-video caches. Deleting it resets the app.

## Architecture

**One process, many threads.** `app.main()` calls `library.init()` then `library.start_background()`, which starts: a folder walker (`index_roots`, stat-only sync of the `media` table), N face-scan workers (`library.worker_loop` → `process_media`: EXIF/video date, dimensions, dhash, YuNet+SFace faces), a CLIP embedding worker (`semantic.worker_loop`), and a face-thumbnail backfill. Flask itself runs `threaded=True`.

- **DB access:** always via `library.db()`, which returns a *thread-local* SQLite connection in autocommit mode (`isolation_level=None`); multi-statement writes use explicit `BEGIN`/`COMMIT`. Schema is `library.SCHEMA` (`CREATE TABLE IF NOT EXISTS`); migrations are ad-hoc in `library.init()` using `PRAGMA table_info` + `ALTER TABLE`, sometimes resetting flags (`scanned`, `meta_read`) so workers redo affected rows. Add new columns the same way.
- **Scan state on `media`:** `scanned` 0 = pending, 1 = done, -1 = failed; `missing=1` marks files that vanished (rows are kept so names/edits survive). `state.priority_folder` makes workers scan the folder the user is viewing first.
- **Version counters as cache invalidation:** `library.state` has `faces_version`, `clip_version`, `assign_version`. Bump the right one after changing faces, CLIP embeddings or name assignments; `faces.FaceIndex` and `semantic.ClipIndex` hold in-memory numpy matrices of embeddings and reload only when their counter changes.
- **`library.fs_lock`** is held by the indexer and by every file move/removal (`tidy._acquire_fs`) so the walker never sees a half-finished move. Any new code that moves files on disk must take it and update `media.path/folder/name` (see `tidy._set_path`) so faces, edits and embeddings follow the file.
- **Background tool jobs:** `tidy.Job` runs one function at a time in a thread with `phase/done/total` progress and keeps the last result in memory. Year check (`tidy.year_job`), duplicates (`tidy.dup_job`) and junk (`junk.junk_job`) follow the same pattern: `POST /api/<tool>` starts, `GET /api/<tool>` polls status + result (the frontend's `runTool` polls), action endpoints apply moves. Results store media ids; `app._with_media` re-hydrates them and drops ones no longer present.
- **Never destroy user files.** Originals are never modified: rotation (number of 90° clockwise turns) and crop live in the `media` row, with `edit_ver` busting thumbnail caches; "Save copy" writes a new `name_edited.*`. Duplicate/junk removal moves files to hidden `.removed-duplicates` / `.removed-junk` folders inside the watched root (recorded in the `removed` table with a `reason`); only the explicit "Delete permanently" action deletes. Year-check moves never overwrite (`tidy._free_path`). Book-art-marked pictures must be refused by removal on the server side.
- **Paths:** watched roots come from `data/config.json` (default: `/mnt/c/Users/*/Pictures` and OneDrive Pictures). `library.normalize_path` maps Windows paths (`D:\Photos`) to `/mnt/d/...`; `library.root_of` finds the longest matching root. Folder-relative logic (year segments, etc.) is computed relative to that root.
- **Models are loaded lazily and optional-friendly:** `faces.FaceEngine` (each worker owns one; request threads share `faces.shared_engine()` under a lock), `semantic.engine()` (CLIP, with its own BPE tokenizer implementation; no `transformers` dependency), `video.ffmpeg_exe()` (ffmpeg on PATH or `imageio-ffmpeg`). Videos the browser can't play are converted to `data/video/<id>.mp4` on demand.

**Module roles:** `imaging.py` (EXIF-aware decoding, thumbnails, edit rendering, video frames/dates, dhash), `faces.py` (detection, suggestions, `<Unknown Person>` clustering; thresholds are constants at the top), `semantic.py` (CLIP search), `tidy.py` (year-check evidence weighting with `W_*` constants and `HIGH`/`MEDIUM` confidence cut-offs, folder year guesses and renames, duplicate finding, holding-folder remove/restore/empty), `junk.py` (zero-shot CLIP `PROMPTS` per category plus file clues; reuses tidy's holding area), `video.py` (ffmpeg jobs).

**Frontend (`static/app.js`):** one file, global state objects `S` (library/left pane), `V` (viewer: zoom, pan, crop, face drawing, video bar) and `T` (tidy tools). `api()`/`post()` wrap fetch and throw on non-2xx using the JSON `error` field, which the server returns for user-facing failures (e.g. 409 when `fs_lock` is busy). Per-viewer UI preferences go through the `store` helper (localStorage, `po.` prefix).

## Conventions

- User-facing text (UI strings, README) is plain, non-technical English addressed to a home user.
- WSL performance matters: files usually live on `/mnt/c`, so avoid reading whole files when headers or `stat` suffice (e.g. duplicates hash only files that share a size).
