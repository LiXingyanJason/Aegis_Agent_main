FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY data ./data
RUN pip install --no-cache-dir .
EXPOSE 9002
ENV EMAIL_MCP_TRANSPORT=streamable-http
ENV EMAIL_MCP_HOST=0.0.0.0
ENV EMAIL_MCP_PORT=9002
CMD ["python", "-m", "aegis_email_mcp"]
