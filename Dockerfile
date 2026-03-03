FROM python:3.14-alpine

ARG TZ=UTC
ENV TZ=$TZ

RUN apk add --no-cache tzdata \
    && cp /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo "$TZ" > /etc/timezone

# Set working directory
WORKDIR /app

# Copy requirements and install Python dependencies
COPY requirements.txt ./
RUN pip3 install --no-cache-dir -r requirements.txt

# Copy scheduler script and make it executable
COPY scheduler.py ./
RUN chmod +x ./scheduler.py

# Run scheduler.py as PID 1 to handle signals directly
ENTRYPOINT ["./scheduler.py"]
