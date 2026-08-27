FROM python:3.12-slim

WORKDIR /app

# Зависимости ставятся отдельным слоем: пересборка после правки кода не тянет
# заново весь pip.
COPY pyproject.toml ./
RUN pip install --no-cache-dir \
    aiogram fastapi "uvicorn[standard]" gspread google-auth apscheduler tenacity

COPY run.py ./
COPY src ./src

ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app/src

CMD ["python", "run.py", "widget"]
