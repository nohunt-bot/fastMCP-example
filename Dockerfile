# 兩階段建置：builder 安裝依賴，runtime 只帶執行需要的東西。
# 在 0.1 core 的節點上，映像檔越小啟動越快。
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app

# 先只複製依賴宣告，讓這一層在原始碼變動時仍能命中快取
# README.md 是 pyproject 的 readme 欄位所宣告的 metadata，
# hatchling 建置本專案時會讀它，少了會建置失敗。
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY skill_server/ ./skill_server/
RUN uv sync --frozen --no-dev


FROM python:3.12-slim AS runtime

# curl 是刻意留下的：bash script 用它打 API 比 python 省 4~5 倍 CPU，
# 而且維運要在容器裡 curl /health 排查問題。
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

# 不以 root 執行。script 是沙箱化的，但深度防禦仍然值得。
RUN useradd --create-home --uid 10001 skill
WORKDIR /app

COPY --from=builder --chown=skill:skill /app/.venv /app/.venv
COPY --chown=skill:skill skill_server/ ./skill_server/
COPY --chown=skill:skill skills/ ./skills/

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# 刻意沒有 VOLUME、沒有可寫目錄：服務不寫任何檔案，
# 可直接搭配 readOnlyRootFilesystem: true 執行。
USER skill
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=30s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

ENTRYPOINT ["skill-mcp"]
CMD ["--host=0.0.0.0", "--port=8000"]
