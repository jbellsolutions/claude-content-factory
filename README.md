# Claude Content Factory

Reusable local repo for turning raw recordings into lead magnets, social-ready assets, and publishable static sites.

## Install

```bash
make deps
cp config/example.env config/.env
```

This repo now has two output layers:

- the existing video lead-magnet pipeline
- a transcript-driven, ready-to-post authority content pack for Facebook, LinkedIn, Medium, Substack, newsletter, and YouTube publishing

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
9. Generate an authority content pack from the transcript when `OPENAI_API_KEY` is available.
10. Optionally create a GitHub repo and publish to GitHub Pages.

## Core idea

There are three layers:

- `scripts/new_job.py`
  - Creates a new job from your source files and a default manifest.
- `scripts/run_job.py`
  - Runs the full pipeline for that job.
- `scripts/watch_dropfolder.py`
  - Watches `inbox/` and automatically creates and runs jobs when new folders arrive.
- `scripts/slack_socket_mode.py`
  - Listens to a Slack channel in Socket Mode, downloads uploaded files, and sends them through the same job runner.
- `scripts/dashboard.py`
  - Runs a local dashboard where you can upload files, monitor jobs, preview outputs, publish to GitHub, and approve distribution.
- `scripts/content_pack.py`
  - Uses transcript material plus Titan-style authority-content DNA to generate ready-to-post Facebook, LinkedIn, Medium, Substack, newsletter, and YouTube assets.
- `scripts/distribute_content.py`
  - Uses Kit and Browser Use adapters to post approved content to configured channels.

That means the automation can later be triggered from:

- Codex running in this repo
- a Finder drop folder
- a Slack bot that saves files into `inbox/` and runs the job automatically
- a dashboard that uploads files and calls the same runner

The runner stays the same. Only the trigger changes.

## Folder layout

- `inbox/`
  - Drop new folders here for auto-processing
- `jobs/<slug>/job.json`
  - Manifest for one job
- `jobs/<slug>/output/`
  - Generated landing page, edited video, PDF, captions, assets, and content pack
- `scripts/new_job.py`
  - Creates a job folder and default manifest
- `scripts/run_job.py`
  - Full build pipeline
- `scripts/publish_job.py`
  - Creates a GitHub repo, pushes the output, and enables Pages
- `scripts/watch_dropfolder.py`
  - Polling watcher for the drop folder
- `scripts/slack_socket_mode.py`
  - Socket Mode Slack listener for channel uploads
- `scripts/dashboard.py`
  - Local browser dashboard for uploads and publishing
- `content_dna/authority_council.json`
  - Embedded Titan-style authority-content routing and voice notes for transcript-based content generation

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

If `OPENAI_API_KEY` is set, the same run will also create:

- `output/content_pack/authority-brief.md`
- `output/content_pack/facebook-post.md`
- `output/content_pack/linkedin-post.md`
- `output/content_pack/linkedin-article.md`
- `output/content_pack/medium-article.md`
- `output/content_pack/substack-post.md`
- `output/content_pack/newsletter.md`
- `output/content_pack/youtube-package.md`
- `output/content_pack/distribution-results.json`

### Slack automation

1. Create a Slack app with Socket Mode enabled.
2. Add these bot scopes:
   - `channels:history` for public channel uploads
   - `groups:history` if you want private channel uploads
   - `files:read` so the bot can call `files.info` and download the uploaded files
3. In Event Subscriptions, subscribe to these bot events:
   - `message.channels`
   - `message.groups` if you want private channel uploads
4. Install the app to your workspace.
5. Copy your tokens into `config/.env`:
   - `SLACK_BOT_TOKEN=xoxb-...`
   - `SLACK_APP_TOKEN=xapp-...`
   - `SLACK_ALLOWED_CHANNELS=C01234567,C07654321`
6. Invite the bot into the channel you want to use.
7. Start the listener:

```bash
make slack
```

Then upload a folder's worth of assets as files in the allowed channel:

- one video file such as `.mp4`
- optional audio sidecar such as `.m4a`
- optional `.vtt`

If you want to override title, checklist, CTA, or repo name from Slack, put it in the message text. Two supported formats:

```text
title: How To Use Claude In 15 Minutes
headline: Build live inside Claude in one short walkthrough
subheadline: Cover Claude Chat, Claude Cowork, and Claude Code in one pass.
repo_name: claude-in-15-minutes-lead-magnet
checklist:
- Know when to use Claude Chat
- Know when to use Claude Cowork
- Know when to use Claude Code
```

or:

```json
{
  "title": "How To Use Claude In 15 Minutes",
  "headline": "Build live inside Claude in one short walkthrough",
  "repo_name": "claude-in-15-minutes-lead-magnet",
  "checklist": [
    "Know when to use Claude Chat",
    "Know when to use Claude Cowork",
    "Know when to use Claude Code"
  ]
}
```

If `SLACK_AUTO_PUBLISH=true`, the listener will also run the GitHub publish step after the build finishes.

If `SLACK_AUTO_APPROVE_POST=true`, the listener will also run the approval-based distribution step after the build finishes.

### Local dashboard

Start the dashboard:

```bash
make dashboard
```

Then open:

```text
http://127.0.0.1:8090
```

What it does:

- upload a source video, optional audio, and optional VTT
- set title, headline, subheadline, lead, checklist, CTA, brand context, and repo name
- queue the build in the background
- preview the generated landing page locally
- open the generated content pack
- publish a completed job to GitHub from the same UI
- approve a completed job for posting and write distribution results back into the run workspace

The dashboard is intentionally local-first. It is the same pipeline with a browser front end, not a separate product.

### Authority content pack

When a transcript is available and `OPENAI_API_KEY` is set, each run also creates an authority-content pack derived from the transcript.

Current outputs:

- Facebook post
- LinkedIn post
- LinkedIn article
- Medium article
- Substack post
- Newsletter edition
- YouTube publishing package

The generated channel files are now intended to be paste-ready, not markdown-first. They use labeled sections, native line breaks, and platform-friendly formatting.

### Approval-based posting

Use `Approve & Post` from the dashboard or run page after reviewing a completed job.

Current distribution behavior:

- Kit newsletter posting uses the official Kit API when `KIT_API_KEY` is configured.
- Browser Use handles browser-driven channels such as Facebook, LinkedIn, Medium, Substack, and optional YouTube Studio upload when `BROWSER_USE_ENABLED=true`.
- Distribution writes its result payload back to `output/content_pack/distribution-results.json`.
- `Queue Local Post` adds a completed hosted run to a durable queue so a local Browser Use worker on your Mac can claim and publish it with your logged-in browser session.

Recommended setup for Browser Use:

- run the dashboard locally on the machine that already has your authenticated Chrome profile
- enable `BROWSER_USE_ENABLED=true`
- leave `BROWSER_USE_STORAGE_STATE` blank to use `Browser.from_system_chrome()`, or point it at a saved storage-state file
- set `BROWSER_USE_OPENAI_MODEL=o3` or another supported Browser Use OpenAI model

Recommended setup for the Railway-to-local posting bridge:

- set `POSTING_WORKER_TOKEN` on Railway and in your local `config/.env`
- set `POSTING_BRIDGE_URL=https://claude-content-factory-web-production.up.railway.app` in your local `config/.env`
- run the local worker with:

```bash
cd /Users/home/claude-content-factory
make post-worker PYTHON=/Users/home/.browser-use-env/bin/python
```

- use `Queue Local Post` on the hosted dashboard after a run completes
- the local worker will claim the approved job, download the generated assets, run the existing posting pipeline locally, and push `distribution-results.json` back into the hosted run

This keeps posting approval explicit while still making the full path from video to published content automatable.

The system is tuned for:

- authority over promotion
- transcript fidelity over invented proof
- strategic clarity over hype
- practical value over sales language

The Titan Genome influence is embedded as a compact local DNA file instead of a runtime dependency on the archived repo.

### Railway deployment

Railway is the better fit than Vercel for this repo because:

- this is a Python process with background jobs
- uploads can be large
- local file generation is central to the workflow
- a persistent volume is useful for `jobs/`, `inbox/`, and dashboard state

Recommended Railway setup:

1. Create a Railway project from this repo.
2. Add a volume and mount it to `/data`.
3. Set environment variables:
   - `CONTENT_FACTORY_DATA_ROOT=/data`
   - `OPENAI_API_KEY=...`
   - optional `OPENAI_MODEL=gpt-5`
   - optional `KIT_FORM_ACTION=...`
   - optional Slack variables if you want Slack ingestion running too
4. Start command:

```bash
python3 scripts/dashboard.py
```

or use:

```bash
make railway-up
```

On Railway, the dashboard binds to `0.0.0.0:$PORT` automatically.

## About automation

If you want "drop a file and it goes," this repo already supports that through the watcher.

If you want "drop it in Slack and it goes," this repo now supports that with `make slack`.

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
