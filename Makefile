PYTHON ?= python3

.PHONY: help new run watch publish

help:
	@echo "make new SLUG=example TITLE='My Lead Magnet' SOURCE_VIDEO=/abs/video.mp4 SOURCE_VTT=/abs/transcript.vtt"
	@echo "make run SLUG=example"
	@echo "make watch"
	@echo "make publish SLUG=example REPO=example-lead-magnet"

new:
	$(PYTHON) scripts/new_job.py \
		--slug "$(SLUG)" \
		--title "$(TITLE)" \
		--source-video "$(SOURCE_VIDEO)" \
		$(if $(SOURCE_AUDIO),--source-audio "$(SOURCE_AUDIO)",) \
		$(if $(SOURCE_VTT),--source-vtt "$(SOURCE_VTT)",)

run:
	$(PYTHON) scripts/run_job.py "jobs/$(SLUG)"

watch:
	$(PYTHON) scripts/watch_dropfolder.py

publish:
	$(PYTHON) scripts/publish_job.py "jobs/$(SLUG)" --repo "$(REPO)"
