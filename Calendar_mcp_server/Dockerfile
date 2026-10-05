FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .
EXPOSE 9001
ENV CALENDAR_MCP_TRANSPORT=streamable-http
ENV CALENDAR_MCP_HOST=0.0.0.0
ENV CALENDAR_MCP_PORT=9001
CMD ["python", "-m", "aegis_calendar_mcp"]
