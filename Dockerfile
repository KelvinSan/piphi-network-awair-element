FROM python:3.12-slim

RUN mkdir /integration \
    && groupadd --system --gid 10001 piphi \
    && useradd --system --uid 10001 --gid piphi --home-dir /nonexistent --shell /usr/sbin/nologin piphi \
    && mkdir -p /data/awair \
    && chown piphi:piphi /data/awair

COPY requirements.txt ./integration/requirements.txt

ARG PIPHI_RUNTIME_KIT_VERSION=0.8.1

RUN pip install --no-cache-dir --upgrade -r /integration/requirements.txt \
    && pip install --no-cache-dir --upgrade "piphi-runtime-kit-python==${PIPHI_RUNTIME_KIT_VERSION}"

COPY ./ /integration

WORKDIR /integration

ENV PIPHI_AUTOMATION_LEDGER_PATH=/data/awair/automation-actions.sqlite3
VOLUME ["/data/awair"]
EXPOSE 3665

USER piphi
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import json, urllib.request; json.load(urllib.request.urlopen('http://127.0.0.1:3665/health', timeout=3))" || exit 1
CMD ["fastapi", "run", "/integration/src/com_piphi_await_element/app.py", "--port", "3665"]
