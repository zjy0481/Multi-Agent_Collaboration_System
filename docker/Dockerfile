FROM python:3.12-slim
RUN groupadd --gid 10001 sandbox && useradd --uid 10001 --gid 10001 --create-home sandbox \
    && mkdir -p /workspace /inputs && chown 10001:10001 /workspace /inputs
WORKDIR /workspace
USER 10001:10001
