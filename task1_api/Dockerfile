FROM python:3.12-slim
WORKDIR /srv
COPY mock_api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY mock_api/ ./mock_api/
EXPOSE 8080
CMD ["uvicorn", "mock_api.app:app", "--host", "0.0.0.0", "--port", "8080"]
