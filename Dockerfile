FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY config.py poe_control_mqtt.py poe_status_to_mqtt.py mqtt_test.py web_ui.py ./
COPY templates ./templates
COPY static ./static

ENV PYTHONUNBUFFERED=1 \
    POE_CONFIG_PATH=/app/poe_control_config.json

CMD ["python", "poe_control_mqtt.py"]
