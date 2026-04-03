# Claude Content Factory

Reusable local repo for turning raw recordings into lead magnets, social-ready assets, and publishable static sites.

## What this repo is for

This repo is the engine. It is meant to stay on your computer and run the same workflow over and over:

1. Drop a new source recording into `inbox/` or initialize a job manually.
2. Build a job folder with its own manifest and output site.
3. Clean transcript text and captions.
4. Reduce filler-only transcript cues and dead space.
5. Render branded intro/outro assets and a poster frame.
6. Build the edited video.
7. Build the PDF companion.
8. Build the landing page with opt-in capture.
9. Optionally create a GitHub repo and publish to GitHub Pages.

## Core idea

There are three layers:

- `scripts/new_job.py`
  - Creates a new job from your source files and a default manifest.
- `scripts/run_job.py`
  - Runs the full pipeline for that job.
- `scripts/watch_dropfolder.py`
  - Watches `inbox/` and automatically creates and runs jobs when new folders arrive.

That means the automation can later be triggered from:

- Codex running in this repo
- a Finder drop folder
- a Slack bot that saves files into `inbox/`
- a dashboard that uploads files and calls the same runner

The runner stays the same. Only the trigger changes.

## Folder layout

- `inbox/`
  - Drop new folders here for auto-processing
- `jobs/<slug>/job.json`
  - Manifest for one job
- `jobs/<slug>/output/`
  - Generated landing page, edited video, PDF, captions, and assets
- `scripts/new_job.py`
  - Creates a job folder and default manifest
- `scripts/run_job.py`
  - Full build pipeline
- `scripts/publish_job.py`
  - Creates a GitHub repo, pushes the output, and enables Pages
- `scripts/watch_dropfolder.py`
  - Polling watcher for the drop folder

## Best current workflow

### Manual

```bash
make new \
  SLUG=claude-in-15-minutes \
  TITLE="How To Use Claude In 15 Minutes" \
  SOURCE_VIDEO=/absolute/path/to/video.mp4 \
  SOURCE_VTT=/absolute/path/to/transcript.vtt

make run SLUG=claude-in-15-minutes
make publish SLUG=claude-in-15-minutes REPO=claude-in-15-minutes-lead-magnet
```

### Automated drop folder

1. Create a folder inside `inbox/`, for example `inbox/my-new-video/`
2. Put these in it:
   - `source.mp4`
   - optional `source.m4a`
   - optional `source.vtt`
3. Optionally add `brief.json` to override title, headline, checklist, CTA, Kit config, or manual segments
4. Run:

```bash
make watch
```

The watcher will:

- create `jobs/my-new-video/`
- copy the inputs into the job
- generate a default manifest if one is missing
- run the pipeline

## About automation

If you want "drop a file and it goes," this repo already supports that through the watcher.

If you want "drop it in Slack and it goes," the next step is a Slack app that saves incoming files into `inbox/` and calls the same runner.

If you want "upload it in a dashboard and it goes," the next step is a lightweight web app that writes the same job files and calls the same runner.

Do not build three separate systems. Use one pipeline and multiple triggers.

## Kit opt-in

The generated landing page includes an email-capture form block.

If `kit_form_action` is present in the job manifest, the page will post to that Kit endpoint.

If it is empty, the page still renders but uses a placeholder form.

## Transcript and filler cleanup

Current automation supports:

- transcript cleanup from VTT
- filler-only cue removal
- dead-space reduction by stitching useful cue groups into segments
- branded captions export

For best results, include a sidecar VTT transcript.

## Social content use

Even when you do not need a full lead magnet page, this repo is still useful because it gives you:

- cleaned transcript text
- captions
- trimmed video
- poster frame
- reusable branded intro/outro

That is enough to support a later YouTube editor or short-form social pipeline.
