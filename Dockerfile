FROM python:3.12-slim
WORKDIR /app
COPY server.py index.html ./
# Nur Standardbibliothek, keine Pakete nötig. Geheimnisse (IGNAV_API_KEY, ACCESS_CODE) als Umgebungsvariablen setzen.
ENV HOST=0.0.0.0 PORT=8787 PUBLIC_MODE=1 TRUST_PROXY=1
EXPOSE 8787
CMD ["python", "server.py"]
