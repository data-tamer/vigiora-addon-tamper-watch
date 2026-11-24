# Built on the Vigiora server from this repository (run.source: build in vigiora-addon.yaml).
#
# The server accepts only a plain subset of the Dockerfile language: every FROM names an image pinned by digest, RUN
# takes no flags, there are no parser directives and no here-documents, ADD and COPY take files of this repository.
# This is the multi-arch index of python:3.12-slim-bookworm (linux/amd64 and linux/arm64). To refresh it:
#   docker buildx imagetools inspect python:3.12-slim-bookworm      and copy the "Digest:" line of the index
FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df

# Where the kit comes from while it has no package index entry (a release tag, not a branch).
ARG KIT_SOURCE=https://github.com/data-tamer/vigiora-addon-kit/archive/refs/tags/v0.2.0.tar.gz

RUN useradd --system --uid 10001 addon && mkdir /data && chown addon /data
WORKDIR /app

# numpy and OpenCV (headless, with its own FFmpeg for RTSP): wheels only, pinned by hash in requirements.txt.
# No apt packages are installed.
COPY requirements.txt ./
RUN pip install --no-cache-dir --only-binary=:all: --require-hashes -r requirements.txt

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir "${KIT_SOURCE}" && pip install --no-cache-dir --no-deps .

USER addon
ENV PYTHONUNBUFFERED=1
CMD ["tamper-watch"]
