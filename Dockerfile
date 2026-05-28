FROM python:3.12-slim

# Install nginx
RUN apt-get update && apt-get install -y --no-install-recommends nginx && \
    rm -rf /var/lib/apt/lists/* && \
    rm -f /etc/nginx/sites-enabled/default /etc/nginx/sites-available/default

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY fetcher.py refresh_server.py .
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY frontend/ /usr/share/nginx/html/

RUN mkdir -p /app/data /var/log/nginx /run/nginx

# Inject VERSION into sw.js template
COPY VERSION /app/VERSION
RUN VERSION=$(cat /app/VERSION) && \
    sed -i "s/{{VERSION}}/$VERSION/g" /usr/share/nginx/html/sw.js

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 80

CMD ["/entrypoint.sh"]
