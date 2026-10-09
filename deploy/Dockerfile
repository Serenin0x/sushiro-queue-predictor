# Official multi-platform Python index, resolved 2026-10-06. Update by reviewed digest.
FROM python:3.13-slim@sha256:bf44cdfcb76cd3b41e879bc058fc37ec5872002ccfde7fcb765e218cde0cd79c AS build
WORKDIR /build
COPY deploy/requirements-build.txt /build/requirements-build.txt
RUN python -m pip install --no-cache-dir --only-binary=:all: --require-hashes -r requirements-build.txt
COPY pyproject.toml README.md LICENSE VERSION /build/
COPY src/ /build/src/
RUN python -m pip wheel . --no-deps --no-build-isolation --wheel-dir /wheels

FROM python:3.13-slim@sha256:bf44cdfcb76cd3b41e879bc058fc37ec5872002ccfde7fcb765e218cde0cd79c
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY deploy/requirements-server.txt /opt/sushiwait/requirements-server.txt
COPY deploy/plan_admin.py /opt/sushiwait/plan_admin.py
COPY config/default-business-hours.json /opt/sushiwait/default-business-hours.json
COPY --from=build /wheels/ /wheels/
RUN python -m pip install --no-cache-dir --only-binary=:all: --require-hashes -r /opt/sushiwait/requirements-server.txt \
    && python -m pip install --no-index --no-deps /wheels/*.whl \
    && python -m pip check \
    && rm -rf /wheels \
    && mkdir /state /state/campaign /plan-updates \
    && printf '{"schema_version":1,"plans":[]}\n' > /state/plans.json \
    && chmod 700 /state /state/campaign /plan-updates && chmod 600 /state/plans.json \
    && chown -R 10001:10001 /state /plan-updates
USER 10001:10001
WORKDIR /state
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=2 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/health',timeout=3).close()"]
ENTRYPOINT ["sushiwait"]
CMD ["remote-window-serve", "--db", "/state/remote.sqlite3", "--task-file", "/state/task.json", "--plan-file", "/state/plans.json", "--store-id", "3014", "--store-id", "3004", "--store-id", "2009", "--base-interval", "300", "--duration", "86400", "--max-pairs", "900", "--resume-if-present", "--listen-host", "0.0.0.0", "--port", "8765"]
