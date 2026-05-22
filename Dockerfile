FROM python:3.12-slim

# Install nginx and cron
RUN apt-get update && apt-get install -y --no-install-recommends nginx cron && \
    rm -rf /var/lib/apt/lists/* && \
    rm -f /etc/nginx/sites-enabled/default /etc/nginx/sites-available/default

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY fetcher.py refresh_server.py .
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY frontend/ /usr/share/nginx/html/

RUN mkdir -p /app/data /var/log/nginx /run/nginx

# crontab is created dynamically in entrypoint.sh to capture runtime env vars
RUN echo "# crontab created by entrypoint.sh" > /etc/cron.d/raynews && \
    chmod 0644 /etc/cron.d/raynews

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 80

CMD ["/entrypoint.sh"]
