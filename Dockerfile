FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src/ ./src/
WORKDIR /app/src
ENV SECURE=false MAV_HOST=0.0.0.0 MAV_PORT=14550
EXPOSE 8137
EXPOSE 14550/udp
# 표적 실행(REST 8137 + C2 MAVLink udp:14550). GNSS 센서 모사 :14600은 localhost 전용이며 EXPOSE하지 않는다.
CMD ["uvicorn", "mock_gcs.app:app", "--host", "0.0.0.0", "--port", "8137"]
