FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY dryrun ./dryrun
ENV TZ=Asia/Kolkata HOST=0.0.0.0 PORT=8000 DATABASE_URL=sqlite:////data/dryrun.db
VOLUME /data
EXPOSE 8000
CMD ["python", "-m", "dryrun"]
