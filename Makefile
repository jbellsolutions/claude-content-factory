PYTHON ?= python3

.PHONY: help deps new run watch slack dashboard post-worker publish

help:
	@echo "make deps"
	@echo "make new SLUG=example TITLE='My Lead Magnet' SOURCE_VIDEO=/abs/video.mp4 SOURCE_VTT=/abs/transcript.vtt"
	@echo "make run SLUG=example"
	@echo "make watch"
	@echo "make slack"
	@echo "make dashboard"
	@echo "make post-worker PYTHON=/Users/home/.browser-use-env/bin/python"
	@echo "make publish SLUG=example REPO=example-lead-magnet"
	@echo "make railway-up"

deps:
	$(PYTHON) -m pip install --user -r requirements.txt

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

slack:
	$(PYTHON) scripts/slack_socket_mode.py

dashboard:
	$(PYTHON) scripts/dashboard.py

post-worker:
	$(PYTHON) scripts/posting_worker.py

railway-up:
	railway up

publish:
	$(PYTHON) scripts/publish_job.py "jobs/$(SLUG)" --repo "$(REPO)"
