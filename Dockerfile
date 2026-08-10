FROM python:3.12-slim

RUN mkdir /integration

COPY requirements.txt ./integration/requirements.txt

ARG PIPHI_RUNTIME_KIT_VERSION=0.6.0

RUN pip install --no-cache-dir --upgrade -r /integration/requirements.txt \
    && pip install --no-cache-dir --upgrade "piphi-runtime-kit-python==${PIPHI_RUNTIME_KIT_VERSION}"

RUN ls

COPY ./ /integration

WORKDIR /integration

EXPOSE 3665

CMD ["fastapi", "run", "/integration/src/com_piphi_await_element/app.py", "--port", "3665"]
