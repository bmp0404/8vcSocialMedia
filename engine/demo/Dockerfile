FROM python:3.12-slim
WORKDIR /app
COPY server.py dashboard.py index.html dashboard.html dashboard.css dashboard.js sample.json /app/
ENV PORT=8080 PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["python", "server.py"]
